"""Local paper ledger: explicit recorded fills, FIFO cost, T+1 availability and risk gates."""
from __future__ import annotations

from pathlib import Path
import uuid

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from . import storage
from .data import parse_csv

INITIAL_CAPITAL = 100000.


class RiskSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    max_position: float = Field(default=.2, gt=0, le=1)
    max_exposure: float = Field(default=.8, gt=0, le=1)
    max_holdings: int = Field(default=5, ge=1, le=20)
    max_drawdown: float = Field(default=.15, gt=0, le=.9)
    daily_loss: float = Field(default=.05, gt=0, le=.9)
    consecutive_loss_days: int = Field(default=3, ge=2, le=10)
    cooldown_sessions: int = Field(default=5, ge=1, le=30)
    stop_loss: float = Field(default=.08, gt=0, le=.9)
    take_profit: float = Field(default=.15, gt=0, le=3)
    trailing_activation: float = Field(default=.1, gt=0, le=3)
    trailing_drawdown: float = Field(default=.05, gt=0, le=.9)
    fee_bps: float = Field(default=5, ge=0, le=100)
    slippage_bps: float = Field(default=2, ge=0, le=100)


def settings() -> dict:
    return RiskSettings(**storage.get_setting('paper_risk', RiskSettings().model_dump())).model_dump()


def trades() -> list[dict]:
    with storage.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM paper_trades ORDER BY trade_date,created_at,id')]


def consume(lots: list[dict], quantity: int, date: str) -> float:
    remaining, cost = quantity, 0.
    for lot in lots:
        if lot['date'] >= date:
            continue
        used = min(lot['quantity'], remaining)
        lot['quantity'] -= used
        cost += used * lot['unit_cost']
        remaining -= used
        if not remaining:
            break
    if remaining:
        raise ValueError('可卖数量不足；模拟账户实行 T+1，同日新增份额不可卖。')
    return cost


def ledger_state(rows: list[dict]) -> tuple[float, dict]:
    cash, lots = INITIAL_CAPITAL, {}
    for row in rows:
        asset = lots.setdefault(row['dataset_id'], [])
        gross = row['quantity'] * row['price']
        if row['side'] == 'BUY':
            cash -= gross + row['fee']
            asset.append({'quantity': row['quantity'], 'unit_cost': (gross + row['fee']) / row['quantity'], 'date': row['trade_date']})
        else:
            consume(asset, row['quantity'], row['trade_date'])
            cash += gross - row['fee']
    return cash, {key: [lot for lot in value if lot['quantity']] for key, value in lots.items() if any(lot['quantity'] for lot in value)}


def portfolio(rows: list[dict] | None = None, as_of=None, risk=None) -> dict:
    rows = trades() if rows is None else rows
    risk = settings() if risk is None else risk
    datasets = {item['id']: storage.get_dataset(item['id']) for item in storage.list_datasets()}
    if not datasets:
        raise ValueError('没有可估值的数据集。')
    as_of = as_of or max(item['end'] for item in datasets.values())
    if rows and as_of < rows[-1]['trade_date']:
        raise ValueError('估值截止日不能早于账户最后一笔记录；历史技术分析仍可独立使用。')
    cash, lots = ledger_state(rows)
    frames = {}
    for dataset_id in {row['dataset_id'] for row in rows}:
        frame = parse_csv(Path(datasets[dataset_id]['path']).read_bytes())
        frames[dataset_id] = frame.loc[frame['date'] <= pd.Timestamp(as_of)].reset_index(drop=True)
    positions = []
    for dataset_id, asset in lots.items():
        frame, meta = frames[dataset_id], datasets[dataset_id]
        quote = frame.iloc[-1]
        quantity = sum(lot['quantity'] for lot in asset)
        total_cost = sum(lot['quantity'] * lot['unit_cost'] for lot in asset)
        average = total_cost / quantity
        mark = float(quote['close'])
        value, pnl = mark * quantity, mark / average - 1
        start = min(lot['date'] for lot in asset)
        peak = float(frame.loc[frame['date'] >= pd.Timestamp(start), 'close'].max())
        flags = []
        if pnl <= -risk['stop_loss']:
            flags.append('达到参考止损线')
        if pnl >= risk['take_profit']:
            flags.append('达到参考止盈线')
        if peak / average - 1 >= risk['trailing_activation'] and mark / peak - 1 <= -risk['trailing_drawdown']:
            flags.append('达到参考移动止盈回撤线')
        positions.append({'dataset_id': dataset_id, 'dataset_name': meta['name'], 'source': meta['source'],
                          'quantity': quantity, 'sellable': sum(lot['quantity'] for lot in asset if lot['date'] < as_of),
                          'average_cost': average, 'close': mark, 'quote_date': quote['date'].strftime('%Y-%m-%d'),
                          'market_value': value, 'unrealized_pnl': value - total_cost, 'return': pnl, 'flags': flags})
    market_value = sum(row['market_value'] for row in positions)
    equity = cash + market_value
    for position in positions:
        position['weight'] = position['market_value'] / equity if equity > 0 else 0.
    history = []
    daily, drawdown = np.array([], dtype=float), np.array([], dtype=float)
    if rows:
        dates = sorted({date for frame in frames.values() for date in frame['date'] if rows[0]['trade_date'] <= date.strftime('%Y-%m-%d') <= as_of})
        index = pd.DatetimeIndex(dates)
        cash_changes = pd.Series(0., index=index)
        equity_series = pd.Series(INITIAL_CAPITAL, index=index)
        for row in rows:
            amount = row['quantity'] * row['price']
            cash_changes.loc[pd.Timestamp(row['trade_date'])] += -amount - row['fee'] if row['side'] == 'BUY' else amount - row['fee']
        equity_series += cash_changes.cumsum()
        for dataset_id, frame in frames.items():
            quantities = pd.Series(0., index=index)
            for row in rows:
                if row['dataset_id'] == dataset_id:
                    quantities.loc[pd.Timestamp(row['trade_date'])] += row['quantity'] * (1 if row['side'] == 'BUY' else -1)
            prices = frame.set_index('date')['close']
            marks = prices.reindex(prices.index.union(index)).sort_index().ffill().reindex(index)
            equity_series += quantities.cumsum() * marks.fillna(0)
        values = equity_series.to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError('模拟估值异常，请核对行情数量级。')
        peaks = np.maximum.accumulate(np.r_[INITIAL_CAPITAL, values])[1:]
        drawdown = values / peaks - 1
        daily = values / np.r_[INITIAL_CAPITAL, values[:-1]] - 1
        history = [{'date': date.strftime('%Y-%m-%d'), 'equity': float(value), 'drawdown': float(dd)}
                   for date, value, dd in zip(index, values, drawdown)]
    blocks = []
    latest_dd = float(drawdown[-1]) if len(drawdown) else 0.
    if len(drawdown):
        breaches = np.flatnonzero((drawdown <= -risk['max_drawdown']) & (np.r_[0, drawdown[:-1]] > -risk['max_drawdown']))
        if len(breaches) and len(drawdown) - 1 - breaches[-1] < risk['cooldown_sessions']:
            blocks.append(f'资产回撤触发后暂停 {risk["cooldown_sessions"]} 个记录日')
    if len(daily) and daily[-1] <= -risk['daily_loss']:
        blocks.append('当日模拟资产亏损达到上限，当前记录日不新增仓位')
    loss_days = 0
    for value in daily[::-1]:
        if value >= 0:
            break
        loss_days += 1
    effective_cap = risk['max_position'] * (.5 if loss_days >= risk['consecutive_loss_days'] else 1)
    return {'as_of': as_of, 'initial_capital': INITIAL_CAPITAL, 'cash': cash, 'market_value': market_value,
            'equity': equity, 'total_return': equity / INITIAL_CAPITAL - 1,
            'exposure': market_value / equity if equity > 0 else 0., 'drawdown': latest_dd,
            'realized_pnl': sum(row['realized_pnl'] for row in rows), 'positions': positions,
            'trades': rows[-200:][::-1], 'history': history[-250:], 'risk': risk,
            'risk_status': {'can_buy': not blocks, 'reasons': blocks, 'consecutive_loss_days': loss_days,
                            'effective_position_cap': effective_cap},
            'assumptions': ['本地模拟账本，初始虚拟资金 100000，不连接券商。',
                            '手动选择记录日，按该日收盘与配置滑点记账；不根据盘中数据成交。',
                            '整数份额、FIFO 成本、双边费用；卖出仅使用更早记录日买入的份额。',
                            '未模拟涨跌停拒单、停牌、最低佣金、税费或最小交易单位；不能视为完整 A 股账户。',
                            '风险触发生成提示或阻止新增买入，不会自动清仓；最新行情日期可能不同。']}


def record_trade(dataset_id: str, side: str, quantity: int, trade_date=None, client_id=None) -> dict:
    if side not in {'BUY', 'SELL'} or isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10000000:
        raise ValueError('模拟方向须为 BUY / SELL，份额须为 1–10000000 的整数。')
    dataset = storage.get_dataset(dataset_id)
    if not dataset:
        raise ValueError('数据集不存在。')
    frame = parse_csv(Path(dataset['path']).read_bytes())
    trade_date = trade_date or frame.iloc[-1]['date'].strftime('%Y-%m-%d')
    quote = frame.loc[frame['date'] == pd.Timestamp(trade_date)]
    if quote.empty:
        raise ValueError('记录日必须是该数据集中已有的行情日期。')
    trade_id = str(client_id or uuid.uuid4())
    risk = settings()
    with storage.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM paper_trades WHERE id=?', (trade_id,)).fetchone()
        if existing:
            if any(existing[key] != value for key, value in [('dataset_id', dataset_id), ('side', side), ('quantity', quantity), ('trade_date', trade_date)]):
                raise ValueError('重复请求标识对应的成交参数不同。')
            return dict(existing)
        rows = [dict(row) for row in db.execute('SELECT * FROM paper_trades ORDER BY trade_date,created_at,id')]
        if rows and trade_date < rows[-1]['trade_date']:
            raise ValueError('新记录日期不能早于账户最后一笔记录；请按时间顺序记账。')
        if len(rows) >= 2000:
            raise ValueError('当前本地演示账本最多保存 2000 笔模拟成交。')
        before = portfolio(rows, trade_date, risk)
        cash, lots = ledger_state(rows)
        close = float(quote.iloc[0]['close'])
        price = close * (1 + risk['slippage_bps'] / 10000 * (1 if side == 'BUY' else -1))
        gross = price * quantity
        fee = gross * risk['fee_bps'] / 10000
        realized = 0.
        if side == 'BUY':
            if before['risk_status']['reasons']:
                raise ValueError('；'.join(before['risk_status']['reasons']))
            if gross + fee > cash + 1e-8:
                raise ValueError('模拟现金不足。')
            held = next((item for item in before['positions'] if item['dataset_id'] == dataset_id), None)
            if not held and len(before['positions']) >= risk['max_holdings']:
                raise ValueError('超过模拟账户最大持仓标的数。')
            after_equity = before['equity'] - (price - close) * quantity - fee
            if ((held['market_value'] if held else 0) + quantity * close) / after_equity > before['risk_status']['effective_position_cap'] + 1e-10:
                raise ValueError('超过当前单标的仓位上限，请减少数量。')
            if (before['market_value'] + quantity * close) / after_equity > risk['max_exposure'] + 1e-10:
                raise ValueError('超过模拟账户总仓位上限。')
        else:
            cost = consume(lots.get(dataset_id, []), quantity, trade_date)
            realized = gross - fee - cost
        now = storage.now()
        db.execute('INSERT INTO paper_trades VALUES(?,?,?,?,?,?,?,?,?)',
                   (trade_id, dataset_id, side, quantity, price, fee, trade_date, realized, now))
    return {'id': trade_id, 'dataset_id': dataset_id, 'side': side, 'quantity': quantity,
            'price': price, 'fee': fee, 'trade_date': trade_date, 'realized_pnl': realized, 'created_at': now}
