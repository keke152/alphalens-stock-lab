"""按时间切分的机器学习实验与下一交易日开盘至收盘回测。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .data import summarize, validate_frame


FEATURE_LABELS = {
    "return_1": "1 日收盘收益",
    "return_2": "2 日动量",
    "return_5": "5 日动量",
    "return_10": "10 日动量",
    "return_20": "20 日动量",
    "overnight_gap": "当日开盘缺口",
    "intraday_return": "当日日内收益",
    "range_ratio": "当日振幅",
    "close_position": "收盘区间位置",
    "ma_gap_5": "5 日均线偏离",
    "ma_gap_20": "20 日均线偏离",
    "volatility_5": "5 日收益波动",
    "volatility_20": "20 日收益波动",
    "volume_ratio_5": "5 日相对成交量",
    "volume_log_change": "成交量对数变化",
    "volume_ratio_20": "20 日相对成交量",
    "rsi_14": "14 日 RSI（0–1）",
    "trend_strength": "动量波动比",
}

DEFAULT_CONFIG = {
    "model": "random_forest",
    "threshold": 0.55,
    "cost_bps": 5.0,
    "slippage_bps": 2.0,
    "initial_capital": 100_000.0,
    "seed": 42,
}


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """每一行只访问该行及之前的数据；滚动窗口包含当前收盘。"""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    daily_return = close.pct_change(fill_method=None)
    features = pd.DataFrame(index=df.index)
    for lag in (1, 2, 5, 10, 20):
        features[f"return_{lag}"] = close.pct_change(lag, fill_method=None)
    features["overnight_gap"] = df["open"] / close.shift(1) - 1
    features["intraday_return"] = close / df["open"] - 1
    features["range_ratio"] = (df["high"] - df["low"]) / close
    daily_range = df["high"] - df["low"]
    features["close_position"] = (close - df["low"]) / daily_range.replace(0, np.nan)
    features["close_position"] = features["close_position"].fillna(0.5)
    features["ma_gap_5"] = close / close.rolling(5).mean() - 1
    features["ma_gap_20"] = close / close.rolling(20).mean() - 1
    features["volatility_5"] = daily_return.rolling(5).std(ddof=1)
    features["volatility_20"] = daily_return.rolling(20).std(ddof=1)
    # +1 允许合法的零成交量，避免用未来数据补值。
    features["volume_ratio_5"] = (volume + 1) / (volume.rolling(5).mean() + 1) - 1
    features["volume_log_change"] = np.log1p(volume).diff()
    features["volume_ratio_20"] = (volume + 1) / (volume.rolling(20).mean() + 1) - 1
    change = close.diff()
    gain = change.clip(lower=0).rolling(14).mean()
    loss = (-change.clip(upper=0)).rolling(14).mean()
    total_movement = gain + loss
    features["rsi_14"] = gain / total_movement.replace(0, np.nan)
    features.loc[total_movement.eq(0), "rsi_14"] = 0.5
    features["trend_strength"] = features["return_5"] / (
        features["volatility_20"] * np.sqrt(5) + 1e-8
    )
    return features[list(FEATURE_LABELS)]


def _config(config: dict) -> dict:
    result = {**DEFAULT_CONFIG, **{k: v for k, v in config.items() if k in DEFAULT_CONFIG}}
    if result["model"] not in {"random_forest", "logistic"}:
        raise ValueError("model 只支持 random_forest 或 logistic。")
    for name in ("threshold", "cost_bps", "slippage_bps", "initial_capital"):
        try:
            result[name] = float(result[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须为数字。") from exc
        if not np.isfinite(result[name]):
            raise ValueError(f"{name} 必须为有限数字。")
    if not 0 <= result["threshold"] <= 1:
        raise ValueError("threshold 必须在 0–1 之间。")
    if not 0 <= result["cost_bps"] <= 100 or not 0 <= result["slippage_bps"] <= 100:
        raise ValueError("单边手续费与滑点须在 0–100 bps 之间。")
    if not 1 <= result["initial_capital"] <= 1e12:
        raise ValueError("initial_capital 必须在 1–1,000,000,000,000 之间。")
    seed = result["seed"]
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed 必须是 0–4294967295 之间的整数。")
    result["seed"] = int(seed)
    return result


def _number(value: Any) -> float | None:
    number = float(value)
    return number if np.isfinite(number) else None


def _date(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _classification(actual: np.ndarray, probabilities: np.ndarray) -> dict:
    direction = (probabilities >= 0.5).astype(int)
    return {
        "accuracy": float(accuracy_score(actual, direction)),
        "auc": float(roc_auc_score(actual, probabilities)) if len(np.unique(actual)) == 2 else None,
        "precision": float(precision_score(actual, direction, zero_division=0)),
        "recall": float(recall_score(actual, direction, zero_division=0)),
    }


def _models(config: dict):
    if config["model"] == "random_forest":
        classifier = RandomForestClassifier(
            n_estimators=180, max_depth=6, min_samples_leaf=6,
            max_features="sqrt", n_jobs=-1, random_state=config["seed"],
        )
        regressor = RandomForestRegressor(
            n_estimators=180, max_depth=6, min_samples_leaf=6,
            max_features=0.8, n_jobs=-1, random_state=config["seed"],
        )
        return classifier, regressor, "随机森林分类 + 收益回归"
    classifier = make_pipeline(
        StandardScaler(), LogisticRegression(C=1.0, max_iter=1500, random_state=config["seed"])
    )
    regressor = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    return classifier, regressor, "逻辑回归分类 + Ridge 收益回归"


def run_experiment(df: pd.DataFrame, config: dict, artifact_path: Path | None = None) -> dict:
    """训练一次并返回完整实验；验证集/测试集均不参与拟合和阈值调优。"""
    settings = _config(config)
    frame = validate_frame(df)
    features = build_features(frame)
    target_return = frame["close"].shift(-1) / frame["open"].shift(-1) - 1
    target_date = frame["date"].shift(-1)
    finite_features = np.isfinite(features.to_numpy(dtype=float)).all(axis=1)
    # 前 20 行是明确的特征预热期，最后一行保留作未知未来预测。
    eligible = finite_features & target_return.notna().to_numpy()
    eligible &= np.isfinite(target_return.fillna(0).to_numpy(dtype=float))
    eligible_indices = np.flatnonzero(eligible)
    if len(eligible_indices) < 250:
        raise ValueError("完成 20 日特征预热后，可训练样本不足 250 行。请检查行情或补充数据。")
    if not finite_features[-1]:
        raise ValueError("最新行情无法生成有限特征，请检查是否存在异常价格或成交量。")
    expected_indices = np.arange(20, len(frame) - 1)
    if not np.array_equal(eligible_indices, expected_indices):
        raise ValueError("预热期后存在无效特征或收益，请检查极端价格、数值溢出及异常行情。")
    x = features.loc[eligible_indices]
    returns = target_return.loc[eligible_indices].to_numpy(dtype=float)
    directions = (returns > 0).astype(int)
    sample_count = len(x)
    train_stop = int(sample_count * 0.6)
    validation_stop = int(sample_count * 0.8)
    positions = {
        "train": np.arange(0, train_stop),
        "validation": np.arange(train_stop + 1, validation_stop),
        "test": np.arange(validation_stop + 1, sample_count),
    }
    split = {"gap": 1}
    for name, position in positions.items():
        split[name] = {
            "start": _date(frame.loc[x.index[position[0]], "date"]),
            "end": _date(frame.loc[x.index[position[-1]], "date"]),
            "rows": int(len(position)),
            "target_start": _date(target_date.loc[x.index[position[0]]]),
            "target_end": _date(target_date.loc[x.index[position[-1]]]),
        }
    train = positions["train"]
    if len(np.unique(directions[train])) < 2:
        raise ValueError("训练区间只有一种涨跌标签，无法训练分类模型；请更换数据或扩大时间范围。")
    classifier, regressor, model_name = _models(settings)
    classifier.fit(x.iloc[train], directions[train])
    regressor.fit(x.iloc[train], returns[train])
    positive_column = int(np.flatnonzero(classifier.classes_ == 1)[0])
    validation = positions["validation"]
    validation_probability = classifier.predict_proba(x.iloc[validation])[:, positive_column]
    validation_metrics = _classification(directions[validation], validation_probability)
    validation_return = regressor.predict(x.iloc[validation])
    validation_metrics["return_mae"] = float(np.mean(np.abs(validation_return - returns[validation])))

    test = positions["test"]
    probability = classifier.predict_proba(x.iloc[test])[:, positive_column]
    predicted_return = regressor.predict(x.iloc[test])
    actual_return = returns[test]
    active = probability >= settings["threshold"]
    fee = settings["cost_bps"] / 10_000
    slippage = settings["slippage_bps"] / 10_000
    # 全额买入允许碎股。买入和卖出分别计费、计滑点；每日收盘后现金持仓。
    gross_factors = (1 + actual_return) * (1 - slippage) * (1 - fee) / ((1 + slippage) * (1 + fee))
    benchmark_returns = gross_factors - 1
    strategy_returns = np.where(active, benchmark_returns, 0.0)
    capital = settings["initial_capital"]
    with np.errstate(over="ignore", invalid="ignore"):
        strategy_equity = capital * np.cumprod(1 + strategy_returns)
        benchmark_equity = capital * np.cumprod(gross_factors)
    if not np.isfinite(strategy_equity).all() or not np.isfinite(benchmark_equity).all():
        raise ValueError("累计收益数值溢出，请检查输入行情是否存在异常数量级。")
    peaks = np.maximum.accumulate(np.r_[capital, strategy_equity])[1:]
    drawdown = strategy_equity / peaks - 1
    total_return = float(strategy_equity[-1] / capital - 1)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        annualized = np.expm1(np.log(strategy_equity[-1] / capital) * 252 / len(test))
    daily_std = float(np.std(strategy_returns, ddof=1)) if len(test) > 1 else 0.0
    sharpe = float(np.mean(strategy_returns) / daily_std * np.sqrt(252)) if daily_std > 1e-12 else 0.0
    trades = int(np.sum(active))
    metrics = _classification(directions[test], probability)
    metrics.update({
        "total_return": total_return,
        "benchmark_return": float(benchmark_equity[-1] / capital - 1),
        "annualized_return": _number(annualized),
        "max_drawdown": float(np.min(drawdown)),
        "sharpe": sharpe,
        "win_rate": float(np.mean(strategy_returns[active] > 0)) if trades else 0.0,
        "trades": trades,
        "test_sessions": int(len(test)),
        "return_mae": float(np.mean(np.abs(predicted_return - actual_return))),
        "exposure": float(np.mean(active)),
    })
    last_x = features.iloc[[-1]]
    latest_probability = float(classifier.predict_proba(last_x)[0, positive_column])
    latest_return = float(regressor.predict(last_x)[0])
    if settings["model"] == "random_forest":
        importance = classifier.feature_importances_
    else:
        coefficients = np.abs(classifier.named_steps["logisticregression"].coef_[0])
        importance = coefficients / coefficients.sum() if coefficients.sum() > 0 else coefficients
    feature_result = sorted([
        {"name": name, "label": FEATURE_LABELS[name], "importance": float(importance[index]),
         "value": float(last_x[name].iloc[0])}
        for index, name in enumerate(features.columns)
    ], key=lambda item: item["importance"], reverse=True)
    prediction_records = []
    equity_records = []
    for offset, position in enumerate(test):
        row_index = x.index[position]
        signal = "BUY" if active[offset] else "WAIT"
        date = _date(target_date.loc[row_index])
        prediction_records.append({
            "signal_date": _date(frame.loc[row_index, "date"]), "date": date,
            "probability_up": float(probability[offset]),
            "predicted_return": float(predicted_return[offset]),
            "actual_return": float(actual_return[offset]), "signal": signal,
            "correct": bool((probability[offset] >= 0.5) == (actual_return[offset] > 0)),
        })
        equity_records.append({
            "date": date, "strategy": float(strategy_equity[offset]),
            "benchmark": float(benchmark_equity[offset]), "drawdown": float(drawdown[offset]),
            "signal": signal, "return": float(strategy_returns[offset]),
            "benchmark_return": float(benchmark_returns[offset]),
        })
    prices = []
    for row in frame.tail(250).itertuples(index=False):
        prices.append({"date": _date(row.date), "open": float(row.open), "high": float(row.high),
                       "low": float(row.low), "close": float(row.close), "volume": float(row.volume)})
    notes = [
        "研究演示，不构成投资建议；模型分数不是收益承诺，也未经概率校准。",
        "t 日收盘后产生信号；目标为下一条行情记录的开盘至收盘收益，最新预测尚无真实标签。",
        "按时间顺序约 60% / 20% / 20% 切分，两个边界各隔离 1 行；日期范围以特征/信号日为准。",
        "分类器、回归器与标准化器仅拟合训练集。验证集仅报告指标，测试集仅做最终评估；未自动调参。",
        "方向准确率、精确率和召回率固定以 0.5 为分类阈值；交易信号使用所选交易阈值。平盘归为非上涨。",
        "策略为日内做多/空仓；基准为每日开盘全额买入、收盘卖出。两者均计双边手续费和滑点，不含隔夜收益。",
        "假定碎股、充分流动性且可日内卖出；未模拟停牌、涨跌停、税费细则与最低佣金。不适用于直接交易 A 股 T+1 股票。",
        "年化按每年 252 个交易日计算；夏普使用零无风险利率和样本日收益标准差；最大回撤以负数表示。",
        "最新预测沿用训练期模型，未使用验证期或测试期重新拟合。特征重要性表示模型内关联，不代表因果关系。",
    ]
    if frame.attrs.get("source") == "synthetic":
        notes.insert(0, "当前为固定种子生成的虚构 OHLCV 数据，仅用于演示流程；不对应真实股票或实盘业绩。")
    else:
        notes.insert(0, "当前为用户导入的历史日线。请自行核对来源、复权口径、交易日顺序与数据使用权限。")
    if metrics["annualized_return"] is None:
        notes.append("年化收益超出数值可表示范围，因此显示为空；请检查异常行情。")
    result = {
        "data_summary": summarize(frame), "split": split, "metrics": metrics,
        "validation_metrics": validation_metrics,
        "latest": {"as_of": _date(frame["date"].iloc[-1]), "probability_up": latest_probability,
                   "predicted_return": latest_return,
                   "signal": "BUY" if latest_probability >= settings["threshold"] else "WAIT",
                   "reference_close": float(frame["close"].iloc[-1]),
                   "model_train_end": split["train"]["target_end"]},
        "features": feature_result, "equity": equity_records, "predictions": prediction_records,
        "prices": prices, "notes": notes,
        "model": {"name": model_name, "threshold": settings["threshold"],
                  "feature_count": len(features.columns), "seed": settings["seed"],
                  "direction_threshold": 0.5, "sklearn_version": sklearn.__version__},
        "config": settings,
    }
    if artifact_path is not None:
        path = Path(artifact_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "classifier": classifier, "regressor": regressor, "feature_names": list(features.columns),
            "feature_labels": FEATURE_LABELS, "config": settings, "split": split,
            "sklearn_version": sklearn.__version__, "data_summary": summarize(frame),
            "model_train_end": result["latest"]["model_train_end"],
            "target": "next_record_close / next_record_open - 1", "format_version": 1,
        }, path, compress=3)
    return result
