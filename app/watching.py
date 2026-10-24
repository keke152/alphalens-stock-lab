"""Seven daily-snapshot rules, deduplicated local alerts and an interruptible monitor."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import storage
from .data import parse_csv
from .technical import indicator_frame, scalar

RULES = ['cost', 'change', 'volume', 'ma_cross', 'rsi', 'gap', 'trailing']


class WatchRules(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    enabled: list[Literal['cost', 'change', 'volume', 'ma_cross', 'rsi', 'gap', 'trailing']] = Field(default_factory=lambda: RULES.copy(), max_length=7)
    profit: float = Field(default=.15, gt=0, le=2)
    loss: float = Field(default=.12, gt=0, le=.9)
    change: float = Field(default=.04, gt=0, le=1)
    volume_high: float = Field(default=2, gt=1, le=20)
    volume_low: float = Field(default=.5, ge=0, lt=1)
    rsi_high: float = Field(default=70, ge=50, le=100)
    rsi_low: float = Field(default=30, ge=0, lt=50)
    gap: float = Field(default=.01, gt=0, le=1)
    trailing_activation: float = Field(default=.1, gt=0, le=2)
    trailing_drawdown: float = Field(default=.05, gt=0, le=.9)

    @model_validator(mode='after')
    def unique_rules(self):
        if len(set(self.enabled)) != len(self.enabled):
            raise ValueError('预警规则不能重复。')
        return self


def watchlist() -> list[dict]:
    with storage.connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM watchlist ORDER BY created_at DESC')]
    for row in rows:
        row['rules'] = json.loads(row['rules'])
        dataset = storage.get_dataset(row['dataset_id'])
        row['dataset_name'] = dataset['name'] if dataset else row['dataset_id']
    return rows


def save_watch(dataset_id: str, cost_price: float | None, cost_date: str | None, rules: WatchRules) -> dict:
    dataset = storage.get_dataset(dataset_id)
    if not dataset:
        raise ValueError('数据集不存在。')
    if cost_price is not None and cost_date is None:
        cost_date = dataset['end']
    if cost_date and not dataset['start'] <= cost_date <= dataset['end']:
        raise ValueError('成本起始日须在数据集日期范围内。')
    with storage.connect() as db:
        db.execute('''INSERT INTO watchlist VALUES(?,?,?,?,?,?) ON CONFLICT(dataset_id) DO UPDATE SET
                      cost_price=excluded.cost_price,cost_date=excluded.cost_date,rules=excluded.rules,updated_at=excluded.updated_at''',
                   (dataset_id, cost_price, cost_date, rules.model_dump_json(), storage.now(), storage.now()))
    return next(row for row in watchlist() if row['dataset_id'] == dataset_id)


def evaluate_rules(frame: pd.DataFrame, rules: WatchRules, cost_price=None, cost_date=None, as_of=None) -> dict:
    selected = frame.loc[frame['date'] <= pd.Timestamp(as_of)].reset_index(drop=True) if as_of else frame.reset_index(drop=True)
    if selected.empty:
        return {'as_of': None, 'events': [], 'close': None, 'error': '截止日前没有行情。'}
    values = indicator_frame(selected)
    row = values.iloc[-1]
    previous = values.iloc[-2] if len(values) > 1 else None
    effective = row['date'].strftime('%Y-%m-%d')
    events = []

    def add(code, title, detail):
        if code in rules.enabled:
            events.append({'rule': code, 'title': title, 'detail': detail})

    cost_available = cost_price is not None and (cost_date is None or cost_date <= effective)
    pnl = float(row['close'] / cost_price - 1) if cost_available else None
    if pnl is not None and (pnl >= rules.profit or pnl <= -rules.loss):
        add('cost', '达到参考成本盈亏阈值', f'参考成本收益 {pnl:.2%}，阈值 +{rules.profit:.1%} / -{rules.loss:.1%}；不含交易费用。')
    if pd.notna(row['change']) and abs(row['change']) >= rules.change:
        add('change', '收盘涨跌幅提醒', f'相对前收盘变化 {row["change"]:.2%}，双向阈值 {rules.change:.1%}。')
    if pd.notna(row['volume_ratio']) and (row['volume_ratio'] >= rules.volume_high or row['volume_ratio'] <= rules.volume_low):
        add('volume', '成交量异常提醒', f'成交量 / 此前20期均量为 {row["volume_ratio"]:.2f}；上限 {rules.volume_high:g}，下限 {rules.volume_low:g}。')
    if previous is not None and pd.notna(previous['ma10']) and pd.notna(row['ma10']):
        up = previous['ma5'] <= previous['ma10'] and row['ma5'] > row['ma10']
        down = previous['ma5'] >= previous['ma10'] and row['ma5'] < row['ma10']
        if up or down:
            add('ma_cross', 'MA5 上穿 MA10' if up else 'MA5 下穿 MA10', f'收盘后确认：MA5={row["ma5"]:.4f}，MA10={row["ma10"]:.4f}。')
    if pd.notna(row['rsi14']) and (row['rsi14'] >= rules.rsi_high or row['rsi14'] <= rules.rsi_low):
        add('rsi', 'RSI 区间提醒', f'简单平均 RSI14={row["rsi14"]:.2f}，阈值 {rules.rsi_low:g} / {rules.rsi_high:g}。')
    if pd.notna(row['gap']) and abs(row['gap']) >= rules.gap:
        add('gap', '开盘跳空提醒', f'开盘相对前收盘缺口 {row["gap"]:.2%}，双向阈值 {rules.gap:.1%}。')
    if cost_available:
        history = selected.loc[selected['date'] >= pd.Timestamp(cost_date)] if cost_date else selected
        peak = float(history['close'].max())
        if peak / cost_price - 1 >= rules.trailing_activation and row['close'] / peak - 1 <= -rules.trailing_drawdown:
            add('trailing', '参考持有区间回撤提醒', f'区间最高收盘 {peak:.4f}，相对成本曾达 {peak/cost_price-1:.2%}，当前从峰值回撤 {row["close"]/peak-1:.2%}。')
    return {'as_of': effective, 'close': float(row['close']), 'cost_return': pnl,
            'volume_ratio': scalar(row['volume_ratio']), 'events': events, 'error': None}


def list_alerts(unread_only=False, as_of=None) -> list[dict]:
    conditions, params = [], []
    if unread_only:
        conditions.append('acknowledged=0')
    if as_of:
        conditions.append('as_of<=?'); params.append(as_of)
    with storage.connect() as db:
        query = 'SELECT * FROM alerts' + (' WHERE ' + ' AND '.join(conditions) if conditions else '') + ' ORDER BY created_at DESC,id DESC LIMIT 200'
        return [dict(row) for row in db.execute(query, params)]


def scan_watchlist(as_of=None) -> dict:
    items, created, matched = [], 0, 0
    for item in watchlist():
        dataset = storage.get_dataset(item['dataset_id'])
        try:
            result = evaluate_rules(parse_csv(Path(dataset['path']).read_bytes()), WatchRules(**item['rules']),
                                    item['cost_price'], item['cost_date'], as_of)
            identity = json.dumps({'dataset_sha256': dataset['sha256'], 'cost_price': item['cost_price'],
                                   'cost_date': item['cost_date'], 'rules': item['rules']}, sort_keys=True)
            with storage.connect() as db:
                for event in result['events']:
                    key = hashlib.sha256((item['dataset_id'] + result['as_of'] + identity + event['rule'] + event['title']).encode()).hexdigest()[:24]
                    cursor = db.execute('INSERT OR IGNORE INTO alerts(id,dataset_id,dataset_name,as_of,rule,title,detail,created_at) VALUES(?,?,?,?,?,?,?,?)',
                                        (key, item['dataset_id'], dataset['name'], result['as_of'], event['rule'], event['title'], event['detail'], storage.now()))
                    created += cursor.rowcount
            matched += len(result['events'])
            items.append({**item, **result})
        except (ValueError, OSError) as exc:
            items.append({**item, 'error': str(exc), 'events': []})
    return {'items': items, 'matched': matched, 'new_alerts': created, 'alerts': list_alerts(),
            'scanned_at': storage.now(), 'source': '本地日线快照，非实时行情'}


class Monitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name='snapshot-monitor', daemon=True)
        self.last_scan = None
        self.last_error = None

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set(); self.wake.set()
        self.thread.join(timeout=10)

    def status(self):
        return {**storage.get_setting('monitor', {'enabled': False, 'interval_seconds': 60}),
                'last_scan': self.last_scan, 'last_error': self.last_error, 'running': self.thread.is_alive()}

    def run(self):
        while not self.stop_event.is_set():
            settings = storage.get_setting('monitor', {'enabled': False, 'interval_seconds': 60})
            if settings['enabled']:
                try:
                    result = scan_watchlist()
                    self.last_scan = result['scanned_at']
                    self.last_error = next((item['error'] for item in result['items'] if item.get('error')), None)
                except Exception:
                    logging.getLogger('alphalens').exception('本地快照检查失败')
                    self.last_error = '检查失败，请查看服务日志。'
            self.wake.wait(settings['interval_seconds'] if settings['enabled'] else 60)
            self.wake.clear()
