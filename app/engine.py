"""Time-ordered holdout or expanding-window experiments with intraday cost accounting."""

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
from .evaluation import backtest, choose_threshold, data_quality, probability_diagnostics, time_splits


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
    "evaluation": "holdout",
    "folds": 3,
    "threshold_mode": "manual",
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
    if result["evaluation"] not in {"holdout", "walk_forward"}:
        raise ValueError("evaluation 只支持 holdout 或 walk_forward。")
    if result["threshold_mode"] not in {"manual", "validation"}:
        raise ValueError("threshold_mode 只支持 manual 或 validation。")
    if isinstance(result["folds"], bool) or not isinstance(result["folds"], int) or not 3 <= result["folds"] <= 5:
        raise ValueError("folds 必须是 3–5 之间的整数。")
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
    """Fit each model on its past training range; select thresholds on validation only."""
    settings = _config(config)
    frame = validate_frame(df)
    features = build_features(frame)
    target_return = frame["close"].shift(-1) / frame["open"].shift(-1) - 1
    target_date = frame["date"].shift(-1)
    finite = np.isfinite(features.to_numpy(dtype=float)).all(axis=1)
    eligible = finite & target_return.notna().to_numpy()
    eligible &= np.isfinite(target_return.fillna(0).to_numpy(dtype=float))
    indices = np.flatnonzero(eligible)
    if len(indices) < 250:
        raise ValueError("完成 20 日特征预热后，可训练样本不足 250 行。")
    if not finite[-1] or not np.array_equal(indices, np.arange(20, len(frame) - 1)):
        raise ValueError("预热期后存在无效特征或收益，请检查异常行情。")
    x = features.loc[indices]
    returns = target_return.loc[indices].to_numpy(dtype=float)
    directions = (returns > 0).astype(int)
    splits = time_splits(len(x), settings["evaluation"], settings["folds"])
    all_probabilities, all_predicted, all_actual, all_active, all_test = [], [], [], [], []
    all_validation_actual, all_validation_probability, all_validation_predicted = [], [], []
    fold_records, fold_ids, selected_thresholds = [], [], []

    def describe(positions: dict) -> dict:
        result = {"gap": 1}
        for name, position in positions.items():
            result[name] = {"start": _date(frame.loc[x.index[position[0]], "date"]),
                            "end": _date(frame.loc[x.index[position[-1]], "date"]),
                            "rows": int(len(position)),
                            "target_start": _date(target_date.loc[x.index[position[0]]]),
                            "target_end": _date(target_date.loc[x.index[position[-1]]])}
        return result

    for fold_index, positions in enumerate(splits, 1):
        train, validation, test = (positions[key] for key in ("train", "validation", "test"))
        if len(np.unique(directions[train])) < 2:
            raise ValueError(f"第 {fold_index} 折训练区间只有一种涨跌标签，请扩大数据范围。")
        classifier, regressor, model_name = _models(settings)
        classifier.fit(x.iloc[train], directions[train])
        regressor.fit(x.iloc[train], returns[train])
        positive_column = int(np.flatnonzero(classifier.classes_ == 1)[0])
        val_probability = classifier.predict_proba(x.iloc[validation])[:, positive_column]
        val_prediction = regressor.predict(x.iloc[validation])
        threshold, threshold_analysis = choose_threshold(val_probability, returns[validation], settings)
        probability = classifier.predict_proba(x.iloc[test])[:, positive_column]
        predicted = regressor.predict(x.iloc[test])
        active = probability >= threshold
        fold_metrics = _classification(directions[test], probability)
        fold_metrics.update(backtest(returns[test], active, settings)["metrics"])
        fold_metrics["return_mae"] = float(np.mean(np.abs(predicted - returns[test])))
        split = describe(positions)
        fold_records.append({"fold": fold_index, "split": split, "threshold": threshold,
                             "metrics": fold_metrics, "threshold_analysis": threshold_analysis})
        all_test.extend(test.tolist())
        all_probabilities.extend(probability.tolist())
        all_predicted.extend(predicted.tolist())
        all_actual.extend(returns[test].tolist())
        all_active.extend(active.tolist())
        fold_ids.extend([fold_index] * len(test))
        selected_thresholds.extend([threshold] * len(test))
        all_validation_actual.extend(returns[validation].tolist())
        all_validation_probability.extend(val_probability.tolist())
        all_validation_predicted.extend(val_prediction.tolist())

    probability = np.asarray(all_probabilities)
    actual_return = np.asarray(all_actual)
    predicted_return = np.asarray(all_predicted)
    active = np.asarray(all_active, dtype=bool)
    test = np.asarray(all_test, dtype=int)
    portfolio = backtest(actual_return, active, settings)
    metrics = _classification((actual_return > 0).astype(int), probability)
    metrics.update(portfolio["metrics"])
    metrics["return_mae"] = float(np.mean(np.abs(predicted_return - actual_return)))
    validation_actual = np.asarray(all_validation_actual)
    validation_metrics = _classification((validation_actual > 0).astype(int), np.asarray(all_validation_probability))
    validation_metrics["return_mae"] = float(np.mean(np.abs(np.asarray(all_validation_predicted) - validation_actual)))
    last_x = features.iloc[[-1]]
    latest_probability = float(classifier.predict_proba(last_x)[0, positive_column])
    latest_return = float(regressor.predict(last_x)[0])
    latest_threshold = fold_records[-1]["threshold"]
    if settings["model"] == "random_forest":
        importance = classifier.feature_importances_
    else:
        weights = np.abs(classifier.named_steps["logisticregression"].coef_[0])
        importance = weights / weights.sum() if weights.sum() else weights
    feature_result = sorted([
        {"name": name, "label": FEATURE_LABELS[name], "importance": float(importance[index]),
         "value": float(last_x[name].iloc[0])} for index, name in enumerate(features.columns)
    ], key=lambda item: item["importance"], reverse=True)
    predictions, equity = [], []
    for offset, position in enumerate(test):
        row_index = x.index[position]
        date = _date(target_date.loc[row_index])
        signal = "BUY" if active[offset] else "WAIT"
        predictions.append({"signal_date": _date(frame.loc[row_index, "date"]), "date": date,
                            "probability_up": float(probability[offset]),
                            "predicted_return": float(predicted_return[offset]),
                            "actual_return": float(actual_return[offset]), "signal": signal,
                            "correct": bool((probability[offset] >= .5) == (actual_return[offset] > 0)),
                            "fold": fold_ids[offset], "threshold": selected_thresholds[offset]})
        equity.append({"date": date, "strategy": float(portfolio["strategy"][offset]),
                       "benchmark": float(portfolio["benchmark"][offset]),
                       "drawdown": float(portfolio["drawdown"][offset]), "signal": signal,
                       "return": float(portfolio["daily"][offset]),
                       "benchmark_return": float(portfolio["benchmark_daily"][offset]),
                       "fold": fold_ids[offset]})
    cost_scenarios = []
    for multiplier in (0, .5, 1, 1.5, 2):
        scenario = {**settings, "cost_bps": settings["cost_bps"] * multiplier,
                    "slippage_bps": settings["slippage_bps"] * multiplier}
        cost_scenarios.append({"multiplier": multiplier, "cost_bps": scenario["cost_bps"],
                               "slippage_bps": scenario["slippage_bps"],
                               **backtest(actual_return, active, scenario)["metrics"]})
    notes = [
        "研究演示；上涨分数未经概率校准，历史回测不能保证未来收益。",
        "t 日收盘后产生信号；目标是下一条行情记录的开盘至收盘收益。",
        "每一折独立拟合分类器、回归器及标准化器；训练/验证/测试边界各隔离 1 行。",
        "分类指标以 0.5 为界，平盘归为非上涨；交易阈值单独记录。",
        "日内做多/空仓，基准每日开盘买入、收盘卖出；均计双边手续费及滑点，无隔夜收益。",
        "假定碎股和充分流动性；未模拟停牌、涨跌停、最低佣金及税费细则，不适用于直接交易 A 股 T+1 股票。",
        "年化按 252 个交易日；夏普使用零无风险利率；最大回撤为负数。",
        "最新预测和特征重要性使用最后一折训练模型，没有全量重训；重要性不代表因果关系。",
        "成本敏感性固定原信号及每折阈值，只改变费用，不据测试表现自动选择参数。",
    ]
    notes.insert(0, "当前为固定种子生成的合成行情，不对应真实证券。" if frame.attrs.get("source") == "synthetic"
                 else "当前为用户导入历史行情，请核对数据来源及复权口径。")
    if settings["evaluation"] == "walk_forward":
        notes.append(f"扩展窗口共 {len(splits)} 折；测试日期连续且不重复，净值跨折延续，资金不重置。后续折允许使用当时已发生的历史数据。")
        notes.append("主界面时间条展示最后一折；完整时间边界见稳健性分析。验证指标汇总各折预测，可能重复包含同一历史日期。")
    else:
        notes.append("单次留出约 60% / 20% / 20%；验证和测试均不参与模型拟合。")
    if settings["threshold_mode"] == "validation":
        notes.append("阈值仅按每折验证期含成本累计收益选择；至少 max(5,ceil(验证行数×10%)) 笔交易，无候选满足时沿用手动阈值，平分取较低阈值。测试集不参与选择。")
    else:
        notes.append("使用手动固定入场阈值，验证期候选表仅供观察，不改变信号。")
    if metrics["annualized_return"] is None:
        notes.append("年化收益超出数值范围，显示为空；请检查异常行情。")
    result = {
        "report_version": 2, "data_summary": summarize(frame), "split": split, "metrics": metrics,
        "validation_metrics": validation_metrics,
        "latest": {"as_of": _date(frame["date"].iloc[-1]), "probability_up": latest_probability,
                   "predicted_return": latest_return,
                   "signal": "BUY" if latest_probability >= latest_threshold else "WAIT",
                   "threshold": latest_threshold, "reference_close": float(frame["close"].iloc[-1]),
                   "model_train_end": split["train"]["target_end"]},
        "features": feature_result, "equity": equity, "predictions": predictions,
        "prices": [{"date": _date(row.date), "open": float(row.open), "high": float(row.high),
                    "low": float(row.low), "close": float(row.close), "volume": float(row.volume)}
                   for row in frame.tail(250).itertuples(index=False)],
        "evaluation": {"mode": settings["evaluation"], "fold_count": len(splits), "gap": 1,
                       "test_start": predictions[0]["date"], "test_end": predictions[-1]["date"],
                       "test_rows": len(predictions), "folds": fold_records},
        "diagnostics": probability_diagnostics(actual_return, probability),
        "data_quality": data_quality(frame), "cost_sensitivity": cost_scenarios,
        "notes": notes, "model": {"name": model_name, "threshold": latest_threshold,
                  "feature_count": len(features.columns), "seed": settings["seed"],
                  "direction_threshold": .5, "sklearn_version": sklearn.__version__}, "config": settings,
    }
    if artifact_path is not None:
        path = Path(artifact_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"classifier": classifier, "regressor": regressor,
                     "feature_names": list(features.columns), "feature_labels": FEATURE_LABELS,
                     "config": settings, "effective_threshold": latest_threshold, "split": split,
                     "sklearn_version": sklearn.__version__, "data_summary": summarize(frame),
                     "model_train_end": result["latest"]["model_train_end"],
                     "target": "next_record_close / next_record_open - 1", "format_version": 2}, path, compress=3)
    return result
