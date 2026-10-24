"""Time boundaries, validation-only threshold selection and shared cost accounting."""
from __future__ import annotations

import numpy as np
import pandas as pd


def time_splits(samples: int, mode: str, folds: int) -> list[dict[str, np.ndarray]]:
    if mode == "holdout":
        train_end, validation_end = int(samples * .6), int(samples * .8)
        return [{"train": np.arange(train_end),
                 "validation": np.arange(train_end + 1, validation_end),
                 "test": np.arange(validation_end + 1, samples)}]
    first_test = int(samples * .6) + 1
    validation_rows = max(20, int(samples * .15))
    blocks = np.array_split(np.arange(first_test, samples), folds)
    result = []
    for test in blocks:
        validation_end = int(test[0]) - 1
        validation_start = validation_end - validation_rows
        train_end = validation_start - 1
        if train_end < 60 or len(test) < 10:
            raise ValueError("滚动评估样本不足，请增加数据量或减少折数。")
        result.append({"train": np.arange(train_end),
                       "validation": np.arange(validation_start, validation_end), "test": test})
    return result


def net_market_returns(actual: np.ndarray, cost_bps: float, slippage_bps: float) -> np.ndarray:
    fee, slip = cost_bps / 10000, slippage_bps / 10000
    return (1 + actual) * (1 - slip) * (1 - fee) / ((1 + slip) * (1 + fee)) - 1


def backtest(actual: np.ndarray, active: np.ndarray, settings: dict) -> dict:
    benchmark = net_market_returns(actual, settings["cost_bps"], settings["slippage_bps"])
    daily = np.where(active, benchmark, 0.0)
    capital = settings["initial_capital"]
    with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
        strategy_equity = capital * np.cumprod(1 + daily)
        benchmark_equity = capital * np.cumprod(1 + benchmark)
        annualized = np.expm1(np.log(strategy_equity[-1] / capital) * 252 / len(actual))
    if not np.isfinite(strategy_equity).all() or not np.isfinite(benchmark_equity).all():
        raise ValueError("累计收益数值溢出，请检查异常行情。")
    if (strategy_equity <= 0).any() or (benchmark_equity <= 0).any():
        raise ValueError("累计净值低于数值精度，请检查异常行情。")
    drawdown = strategy_equity / np.maximum.accumulate(np.r_[capital, strategy_equity])[1:] - 1
    std = float(np.std(daily, ddof=1)) if len(actual) > 1 else 0.0
    trades = int(np.sum(active))
    return {"daily": daily, "benchmark_daily": benchmark, "strategy": strategy_equity,
            "benchmark": benchmark_equity, "drawdown": drawdown,
            "metrics": {"total_return": float(strategy_equity[-1] / capital - 1),
                        "benchmark_return": float(benchmark_equity[-1] / capital - 1),
                        "annualized_return": float(annualized) if np.isfinite(annualized) else None,
                        "max_drawdown": float(drawdown.min()),
                        "sharpe": float(daily.mean() / std * np.sqrt(252)) if std > 1e-12 else 0.0,
                        "win_rate": float(np.mean(daily[active] > 0)) if trades else 0.0,
                        "trades": trades, "test_sessions": len(actual), "exposure": float(active.mean())}}


def choose_threshold(probabilities: np.ndarray, actual: np.ndarray, settings: dict) -> tuple[float, dict]:
    """Uses validation outcomes only. Manual threshold is the explicit fallback."""
    candidates = sorted(set([round(.5 + index * .05, 2) for index in range(9)] + [settings["threshold"]]))
    min_trades = max(5, int(np.ceil(len(actual) * .1)))
    rows = []
    for threshold in candidates:
        metrics = backtest(actual, probabilities >= threshold, settings)["metrics"]
        rows.append({"threshold": threshold, "trades": metrics["trades"],
                     "total_return": metrics["total_return"], "max_drawdown": metrics["max_drawdown"],
                     "eligible": metrics["trades"] >= min_trades})
    selected = settings["threshold"]
    fallback = False
    if settings["threshold_mode"] == "validation":
        eligible = [row for row in rows if row["eligible"]]
        if eligible:
            # Ties prefer the lower threshold, a predeclared deterministic rule.
            selected = max(eligible, key=lambda row: (row["total_return"], -row["threshold"]))["threshold"]
        else:
            fallback = True
    return selected, {"source": "validation", "mode": settings["threshold_mode"],
                      "selected": selected, "minimum_trades": min_trades, "fallback": fallback,
                      "objective": "validation_net_total_return", "candidates": rows}


def probability_diagnostics(actual: np.ndarray, probability: np.ndarray) -> dict:
    labels = (actual > 0).astype(int)
    predicted = probability >= .5
    bins = []
    for index in range(5):
        low, high = index / 5, (index + 1) / 5
        mask = (probability >= low) & ((probability <= high) if index == 4 else (probability < high))
        bins.append({"lower": low, "upper": high, "count": int(mask.sum()),
                     "mean_probability": float(probability[mask].mean()) if mask.any() else None,
                     "observed_up_rate": float(labels[mask].mean()) if mask.any() else None})
    return {"brier_score": float(np.mean((probability - labels) ** 2)),
            "up_rate": float(labels.mean()),
            "confusion": {"tn": int(np.sum(~predicted & (labels == 0))),
                          "fp": int(np.sum(predicted & (labels == 0))),
                          "fn": int(np.sum(~predicted & (labels == 1))),
                          "tp": int(np.sum(predicted & (labels == 1)))}, "reliability_bins": bins}


def data_quality(frame: pd.DataFrame) -> dict:
    change = frame["close"].pct_change(fill_method=None)
    gaps = frame["date"].diff().dt.days
    checks = [
        {"code": "zero_volume", "label": "零成交量记录", "count": int(frame["volume"].eq(0).sum()),
         "detail": "可能是停牌或缺失成交量；回测未模拟无法成交。"},
        {"code": "large_change", "label": "收盘价单日变化超过 20%", "count": int(change.abs().gt(.2).sum()),
         "detail": "请核对复权、公司行动或异常行情；不自动删除。"},
        {"code": "long_gap", "label": "相邻记录间隔超过 7 个自然日", "count": int(gaps.gt(7).sum()),
         "detail": "可能是节假日、停牌或缺失记录；未使用交易日历判断。"},
    ]
    return {"structural_validation": "passed", "rows": len(frame),
            "warning_count": sum(check["count"] > 0 for check in checks), "checks": checks,
            "scope": "对整份输入文件做描述性检查；不会修改样本或参与参数选择。"}
