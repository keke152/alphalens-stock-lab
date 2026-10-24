"""Causal daily indicators and transparent research templates, independent of ML scores."""
from __future__ import annotations

import numpy as np
import pandas as pd


def indicator_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    close, volume = frame['close'], frame['volume']
    for window in (5, 10, 20, 60):
        result[f'ma{window}'] = close.rolling(window).mean()
    change = close.diff()
    gains, losses = change.clip(lower=0).rolling(14).mean(), (-change.clip(upper=0)).rolling(14).mean()
    result['rsi14'] = 100 * gains / (gains + losses).replace(0, np.nan)
    result.loc[(gains + losses).eq(0), 'rsi14'] = 50
    # Explicit EMA seed: first observed close, adjust=False. Display after 26 records.
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    result['dif'], result['dea'], result['macd'] = dif, dea, 2 * (dif - dea)
    result.loc[result.index[:25], ['dif', 'dea', 'macd']] = np.nan
    deviation = close.rolling(20).std(ddof=0)
    result['boll_mid'] = result['ma20']
    result['boll_upper'], result['boll_lower'] = result['ma20'] + 2 * deviation, result['ma20'] - 2 * deviation
    low, high = frame['low'].rolling(9).min(), frame['high'].rolling(9).max()
    rsv = 100 * (close - low) / (high - low).replace(0, np.nan)
    rsv = rsv.mask((high - low).eq(0), 50)
    # K/D initialize at 50; the first eight rows are warmup, not fabricated values.
    k, d, kvals, dvals = 50., 50., [], []
    for value in rsv:
        if pd.isna(value):
            kvals.append(np.nan); dvals.append(np.nan)
        else:
            k = 2 * k / 3 + value / 3
            d = 2 * d / 3 + k / 3
            kvals.append(k); dvals.append(d)
    result['k'], result['d'] = kvals, dvals
    result['j'] = 3 * result['k'] - 2 * result['d']
    tr = pd.concat([frame['high'] - frame['low'], (frame['high'] - close.shift()).abs(),
                    (frame['low'] - close.shift()).abs()], axis=1).max(axis=1)
    result['atr14'] = tr.rolling(14).mean()
    result['volume_ratio'] = volume / volume.shift().rolling(20).mean().replace(0, np.nan)
    result['change'] = close.pct_change(fill_method=None)
    result['gap'] = frame['open'] / close.shift() - 1
    return result


def scalar(value):
    return float(value) if value is not None and np.isfinite(value) else None


def technical_score(row: pd.Series) -> dict:
    definitions = [
        ('trend', '均线趋势', 25, ['ma5', 'ma20', 'ma60'], lambda: row['close'] > row['ma20'] > row['ma60']),
        ('momentum', '温和动量', 20, ['rsi14'], lambda: 45 <= row['rsi14'] <= 70),
        ('macd', 'MACD 强度', 20, ['dif', 'dea'], lambda: row['dif'] > row['dea']),
        ('volatility', '波动约束', 15, ['atr14'], lambda: row['atr14'] / row['close'] < .04),
        ('volume', '量能配合', 20, ['volume_ratio'], lambda: 1 <= row['volume_ratio'] <= 3),
    ]
    components, points = [], 0
    for code, label, weight, fields, check in definitions:
        available = all(pd.notna(row[field]) and np.isfinite(row[field]) for field in fields)
        passed = bool(check()) if available else None
        points += weight if passed else 0
        components.append({'code': code, 'label': label, 'weight': weight, 'passed': passed,
                           'values': {field: scalar(row[field]) for field in fields}})
    available = all(item['passed'] is not None for item in components)
    value = points if available else None
    return {'value': value, 'label': '偏强' if value is not None and value >= 70 else '中性' if value is not None and value >= 40 else '偏弱' if value is not None else '预热不足',
            'components': components, 'scope': '技术规则分，非模型上涨概率或收益预测',
            'overall_score': None, 'coverage': .25 if available else 0,
            'dimensions': [{'name': name, 'weight': weight, 'status': 'available' if name == '技术面' and available else 'missing',
                            'score': value if name == '技术面' else None}
                           for name, weight in [('技术面', .25), ('资金面', .30), ('基本面', .10), ('消息面', .20), ('情绪面', .15)]]}


def analysis(frame: pd.DataFrame, as_of: str | None = None) -> dict:
    selected = frame.loc[frame['date'] <= pd.Timestamp(as_of)].reset_index(drop=True) if as_of else frame.reset_index(drop=True)
    if selected.empty:
        raise ValueError('截止日之前没有行情记录。')
    values = indicator_frame(selected)
    latest = values.iloc[-1]
    rows = []
    for _, row in values.tail(250).iterrows():
        rows.append({key: row[key].strftime('%Y-%m-%d') if key == 'date' else scalar(row[key]) for key in values.columns})
    return {'as_of': latest['date'].strftime('%Y-%m-%d'), 'requested_as_of': as_of,
            'rows_available': len(values), 'latest': rows[-1], 'series': rows,
            'score': technical_score(latest),
            'notes': ['仅使用截止日及以前的本地日线；指标不触发真实交易。',
                      'RSI 使用 14 期简单平均，ATR 使用 14 期简单平均；与 Wilder 平滑版本可能不同。',
                      'MACD 柱为 2×(DIF−DEA)；KDJ 初值 50；布林带使用 20 期总体标准差。']}


def scan_templates(frame: pd.DataFrame, *, as_of=None, lookback=250, volume_multiple=1.5,
                   rsi_limit=85., event_threshold=.1, benchmark: pd.DataFrame | None = None,
                   held=False, cooldown=False) -> dict:
    selected = frame.loc[frame['date'] <= pd.Timestamp(as_of)].reset_index(drop=True) if as_of else frame.reset_index(drop=True)
    if selected.empty:
        raise ValueError('截止日之前没有行情记录。')
    values = indicator_frame(selected)
    row = values.iloc[-1]
    enough = len(values) > lookback
    high = scalar(values['close'].iloc[-lookback-1:-1].max()) if enough else None
    benchmark_ok = None
    if benchmark is not None:
        b = benchmark.loc[benchmark['date'] <= row['date']]
        if len(b) >= 20 and b.iloc[-1]['date'] == row['date']:
            benchmark_ok = bool(b.iloc[-1]['close'] > b['close'].tail(20).mean())
    conditions = [
        {'label': f'收盘突破此前 {lookback} 条记录最高收盘', 'passed': bool(row['close'] > high) if enough else None, 'value': scalar(row['close']), 'reference': high},
        {'label': 'MA5 > MA20，收盘站上两条均线', 'passed': bool(row['ma5'] > row['ma20'] and row['close'] > max(row['ma5'], row['ma20'])) if pd.notna(row['ma20']) else None},
        {'label': f'成交量 ≥ 前20期均量 × {volume_multiple:g}', 'passed': bool(row['volume_ratio'] >= volume_multiple) if pd.notna(row['volume_ratio']) else None, 'value': scalar(row['volume_ratio'])},
        {'label': f'RSI14 < {rsi_limit:g}', 'passed': bool(row['rsi14'] < rsi_limit) if pd.notna(row['rsi14']) else None, 'value': scalar(row['rsi14'])},
        {'label': '参考指数同日收盘 > MA20', 'passed': benchmark_ok},
        {'label': '模拟账户未持有且未处于风控暂停', 'passed': not held and not cooldown},
    ]
    changes = values['change'].fillna(0).to_numpy()
    streak = 0
    for change in changes[::-1]:
        if change + 1e-12 < event_threshold:
            break
        streak += 1
    known = all(item['passed'] is not None for item in conditions)
    return {'as_of': row['date'].strftime('%Y-%m-%d'), 'conditions': conditions,
            'matched': known and all(item['passed'] for item in conditions),
            'status': '条件通过' if known and all(item['passed'] for item in conditions) else '数据不足' if not known else '条件未通过',
            'passed_count': sum(item['passed'] is True for item in conditions),
            'event': {'threshold': event_threshold, 'streak': streak, 'change': scalar(row['change']),
                      'matched': streak > 0, 'scope': '按自定义收盘涨幅统计事件，不识别真实涨停价、封单或次日晋级概率'}}
