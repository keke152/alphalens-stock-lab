"""Research tools built from registered local snapshots and an isolated paper ledger."""
from __future__ import annotations

import csv
from datetime import date
import io
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import paper, storage, watching
from .data import parse_csv
from .technical import analysis, scan_templates
from .reporting import render_daily_review

router = APIRouter(prefix='/api', tags=['研究工具'])


def dataset_frame(dataset_id):
    dataset = storage.get_dataset(dataset_id)
    if not dataset:
        raise HTTPException(404, '数据集不存在。')
    try:
        return dataset, parse_csv(Path(dataset['path']).read_bytes())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, '本地行情文件不可读取，请重新导入CSV。') from exc


def value_error(action):
    try:
        return action()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class WatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    dataset_id: str = Field(min_length=1, max_length=100)
    cost_price: float | None = Field(default=None, gt=0, le=1e9)
    cost_date: date | None = None
    rules: watching.WatchRules = Field(default_factory=watching.WatchRules)


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    as_of: date | None = None


class ScanRequest(SnapshotRequest):
    dataset_ids: list[str] | None = Field(default=None, max_length=50)
    benchmark_dataset_id: str | None = Field(default=None, max_length=100)
    lookback: int = Field(default=250, ge=20, le=500)
    volume_multiple: float = Field(default=1.5, ge=1, le=10, allow_inf_nan=False)
    rsi_limit: float = Field(default=85, gt=0, le=100, allow_inf_nan=False)
    event_threshold: float = Field(default=.1, ge=.01, le=.5, allow_inf_nan=False)


class MonitorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    interval_seconds: int = Field(default=60, ge=30, le=3600)


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    acknowledged: bool = True


class TradeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1, max_length=100)
    side: Literal['BUY', 'SELL']
    quantity: int = Field(ge=1, le=10000000, strict=True)
    trade_date: date | None = None
    client_id: UUID | None = None


@router.get('/datasets/{dataset_id}/analysis')
def technical_analysis(dataset_id: str, as_of: date | None = None):
    dataset, frame = dataset_frame(dataset_id)
    result = value_error(lambda: analysis(frame, as_of.isoformat() if as_of else None))
    return {**result, 'dataset': storage.public_dataset(dataset)}


@router.get('/research-tools')
def research_tools(request: Request, as_of: date | None = None):
    items = watching.watchlist()
    for item in items:
        _, frame = dataset_frame(item['dataset_id'])
        item['snapshot'] = watching.evaluate_rules(frame, watching.WatchRules(**item['rules']), item['cost_price'], item['cost_date'], as_of)
    return {'watchlist': items, 'alerts': watching.list_alerts(), 'monitor': request.app.state.monitor.status(),
            'rule_defaults': watching.WatchRules().model_dump(),
            'data_sources': [{'name': '内置合成日线', 'status': 'available', 'detail': '可复现的虚构行情'},
                             {'name': '本地 OHLCV CSV', 'status': 'available', 'detail': '按用户导入的日线快照分析'},
                             {'name': '资金流 / 财务 / 新闻 / 情绪', 'status': 'missing', 'detail': '未接入；不生成对应分数'},
                             {'name': '券商与外部消息推送', 'status': 'missing', 'detail': '当前使用模拟账本与站内预警'}]}


@router.put('/watchlist')
def put_watch(body: WatchRequest):
    return value_error(lambda: watching.save_watch(body.dataset_id, body.cost_price,
                       body.cost_date.isoformat() if body.cost_date else None, body.rules))


@router.get('/research-tools/export')
def export_review(dataset_id: str, request: Request, as_of: date | None = None,
                  format: Literal['html', 'json'] = 'html'):
    snapshot = technical_analysis(dataset_id, as_of)
    cutoff = snapshot['as_of']
    workspace = research_tools(request, date.fromisoformat(cutoff))
    workspace['alerts'] = watching.list_alerts(as_of=cutoff)
    rows = [row for row in paper.trades() if row['trade_date'] <= cutoff]
    account = value_error(lambda: paper.portfolio(rows, cutoff))
    review = {'as_of': cutoff, 'generated_at': storage.now(), 'analysis': snapshot,
              'workspace': workspace, 'paper': account,
              'notes': ['研究日报基于本地已有日线，不包含实时行情或未接入的数据维度。',
                        '自选清单和风控参数使用当前配置，历史截止日用于过滤行情、预警和成交，不代表当时保存的原始配置。',
                        '模拟持仓按截止日及以前的账本重建，初始虚拟资金为100000，与机器学习日内回测独立。']}
    content = json.dumps(review, ensure_ascii=False, allow_nan=False, indent=2) if format == 'json' else render_daily_review(review)
    return Response(content, media_type='application/json' if format == 'json' else 'text/html',
                    headers={'Content-Disposition': f'attachment; filename="alphalens-review-{cutoff}.{format}"'})


@router.delete('/watchlist/{dataset_id}', status_code=204)
def delete_watch(dataset_id: str):
    with storage.connect() as db:
        cursor = db.execute('DELETE FROM watchlist WHERE dataset_id=?', (dataset_id,))
    if not cursor.rowcount:
        raise HTTPException(404, '自选项不存在。')
    return Response(status_code=204)


@router.post('/watchlist/scan')
def scan_watch(body: SnapshotRequest):
    return watching.scan_watchlist(body.as_of.isoformat() if body.as_of else None)


@router.patch('/alerts/{alert_id}')
def acknowledge(alert_id: str, body: AcknowledgeRequest):
    with storage.connect() as db:
        cursor = db.execute('UPDATE alerts SET acknowledged=? WHERE id=?', (int(body.acknowledged), alert_id))
    if not cursor.rowcount:
        raise HTTPException(404, '预警不存在。')
    return {'id': alert_id, 'acknowledged': body.acknowledged}


def csv_download(filename, fields, rows):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader(); writer.writerows(rows)
    return Response(buffer.getvalue().encode('utf-8-sig'), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('/alerts-export')
def export_alerts():
    rows = watching.list_alerts()
    # CSV cells derived from uploaded dataset names remain text in spreadsheet apps.
    for row in rows:
        if row['dataset_name'].startswith(('=', '+', '-', '@', '\t', '\r')):
            row['dataset_name'] = "'" + row['dataset_name']
    return csv_download('alphalens-alerts.csv', ['as_of', 'dataset_id', 'dataset_name', 'rule', 'title', 'detail', 'acknowledged', 'created_at'], rows)


@router.put('/monitor')
def configure_monitor(body: MonitorRequest, request: Request):
    storage.set_setting('monitor', body.model_dump())
    request.app.state.monitor.wake.set()
    return request.app.state.monitor.status()


@router.post('/scans')
def scan_strategies(body: ScanRequest):
    ids = list(dict.fromkeys(body.dataset_ids)) if body.dataset_ids is not None else [row['id'] for row in storage.list_datasets()]
    benchmark = dataset_frame(body.benchmark_dataset_id)[1] if body.benchmark_dataset_id else None
    as_of = body.as_of.isoformat() if body.as_of else None
    # A historical technical scan must not peek into future account records.
    rows = [row for row in paper.trades() if as_of is None or row['trade_date'] <= as_of]
    account = value_error(lambda: paper.portfolio(rows, as_of))
    held = {row['dataset_id'] for row in account['positions']}
    results = []
    for dataset_id in ids:
        dataset, frame = dataset_frame(dataset_id)
        try:
            scan = scan_templates(frame, as_of=as_of, lookback=body.lookback, volume_multiple=body.volume_multiple,
                                  rsi_limit=body.rsi_limit, event_threshold=body.event_threshold, benchmark=benchmark,
                                  held=dataset_id in held, cooldown=not account['risk_status']['can_buy'])
            results.append({'dataset_id': dataset_id, 'dataset_name': dataset['name'], 'source': dataset['source'], **scan})
        except ValueError as exc:
            results.append({'dataset_id': dataset_id, 'dataset_name': dataset['name'], 'error': str(exc)})
    return {'results': results, 'config': body.model_dump(mode='json'), 'scanned_at': storage.now(),
            'notes': ['新高为此前指定记录数的收盘最高，不等于上市以来新高。',
                      '涨幅事件使用自定义阈值，不识别交易所涨停规则、封单资金或预测次日晋级。',
                      '未提供同日指数数据时，对应条件标记未知，不算作通过。扫描结果不会自动下单。']}


@router.get('/paper')
def paper_account(as_of: date | None = None):
    return value_error(lambda: paper.portfolio(as_of=as_of.isoformat() if as_of else None))


@router.put('/paper/risk')
def set_risk(body: paper.RiskSettings):
    storage.set_setting('paper_risk', body.model_dump())
    return body.model_dump()


@router.post('/paper/trades', status_code=201)
def add_trade(body: TradeRequest):
    return value_error(lambda: paper.record_trade(body.dataset_id, body.side, body.quantity,
                       body.trade_date.isoformat() if body.trade_date else None,
                       str(body.client_id) if body.client_id else None))


@router.get('/paper/export')
def export_trades():
    return csv_download('alphalens-paper-ledger.csv', ['id', 'dataset_id', 'side', 'quantity', 'price', 'fee', 'trade_date', 'realized_pnl', 'created_at'], paper.trades())
