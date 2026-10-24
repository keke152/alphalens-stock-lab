"""离线行情数据：可复现的虚构样本与严格的日线 CSV 导入。"""

from __future__ import annotations

import csv
import io

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
MIN_ROWS = 300
MAX_ROWS = 20_000
MAX_CSV_BYTES = 8 * 1024 * 1024
DEMO_SYMBOLS = {
    "DEMO.A": {"seed": 41, "price": 80.0, "noise": 0.009, "momentum": 0.22},
    "DEMO.B": {"seed": 137, "price": 45.0, "noise": 0.014, "momentum": -0.18},
    "DEMO.C": {"seed": 701, "price": 120.0, "noise": 0.007, "momentum": 0.08},
}


def validate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """返回按交易日期排列的副本；拒绝缺失、重复及不一致的 OHLC。"""
    if frame.columns.duplicated().any():
        raise ValueError("行情数据列名重复，请检查表头。")
    missing = [name for name in REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError("CSV 缺少必要列：" + ", ".join(missing))
    if not MIN_ROWS <= len(frame) <= MAX_ROWS:
        raise ValueError(f"需要 {MIN_ROWS}–{MAX_ROWS} 行日线数据，当前为 {len(frame)} 行。")
    # 日期不重复也可能是混在一起的不同股票，保留额外标识列做检查后再裁剪。
    for column in ("symbol", "ticker", "code", "ts_code", "证券代码", "股票代码"):
        if column in frame.columns:
            identifiers = frame[column].astype("string").str.strip().replace("", pd.NA).dropna()
            if identifiers.nunique() > 1:
                raise ValueError(f"{column} 列包含多个标的，请按单个股票分别导入 CSV。")
    result = frame[REQUIRED_COLUMNS].copy()
    attrs = frame.attrs.copy()
    if pd.api.types.is_datetime64_any_dtype(result["date"]):
        if getattr(result["date"].dt, "tz", None) is not None:
            raise ValueError("date 应为不带时区的交易日期。")
        dates = result["date"]
        if (dates.dropna() != dates.dropna().dt.normalize()).any():
            raise ValueError("仅支持日线数据，date 不应包含时分秒。")
    else:
        date_text = result["date"].astype("string").str.strip()
        valid_format = date_text.str.fullmatch(r"\d{4}-\d{2}-\d{2}", na=False)
        if not valid_format.all():
            raise ValueError("date 必须使用 YYYY-MM-DD 格式，例如 2026-01-05。")
        dates = pd.to_datetime(date_text, format="%Y-%m-%d", errors="coerce")
    if dates.isna().any():
        raise ValueError("date 包含空值或不存在的日期。")
    result["date"] = dates
    if result["date"].duplicated().any():
        duplicate = result.loc[result["date"].duplicated(), "date"].iloc[0]
        raise ValueError(f"交易日期重复：{duplicate:%Y-%m-%d}。请导入单个标的的日线。")

    for column in REQUIRED_COLUMNS[1:]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
        values = result[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"{column} 列包含空值、非数字或无穷值。")
    if (result[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("open、high、low、close 必须全部大于 0。")
    if (result["volume"] < 0).any():
        raise ValueError("volume 不能为负数；无成交日可填 0。")
    invalid_ohlc = (
        (result["high"] < result[["open", "close", "low"]].max(axis=1))
        | (result["low"] > result[["open", "close", "high"]].min(axis=1))
    )
    if invalid_ohlc.any():
        invalid_date = result.loc[invalid_ohlc, "date"].iloc[0]
        raise ValueError(f"{invalid_date:%Y-%m-%d} 的 OHLC 不一致：high 应为最高价，low 应为最低价。")
    result = result.sort_values("date", kind="stable").reset_index(drop=True)
    result.attrs.update(attrs)
    return result


def generate_demo(symbol: str = "DEMO.A", rows: int = 1000) -> pd.DataFrame:
    """生成虚构日线，固定随机种子；它们不是任何真实证券的行情。"""
    if symbol not in DEMO_SYMBOLS:
        raise ValueError("演示标的不存在，可选 DEMO.A、DEMO.B、DEMO.C。")
    if isinstance(rows, bool) or not isinstance(rows, (int, np.integer)):
        raise ValueError("rows 必须是整数。")
    if not MIN_ROWS <= rows <= MAX_ROWS:
        raise ValueError(f"演示行数须在 {MIN_ROWS}–{MAX_ROWS} 之间。")
    settings = DEMO_SYMBOLS[symbol]
    rng = np.random.default_rng(settings["seed"])
    dates = pd.bdate_range(end="2026-10-21", periods=rows)
    history = []
    previous_close = settings["price"]
    previous_intraday = 0.0
    previous_volume_surprise = 0.0
    for index, date in enumerate(dates):
        # 随机波动和缓慢变化的市场环境，保留少量可学习的滞后关系。
        regime = np.sin(index / 95.0)
        volatility = settings["noise"] * (1.0 + 0.32 * np.sin(index / 31.0) ** 2)
        gap = float(np.clip(rng.normal(0.0, volatility * 0.38), -0.07, 0.07))
        open_price = previous_close * np.exp(gap)
        predictable = (
            settings["momentum"] * previous_intraday
            + 0.0008 * np.tanh(previous_volume_surprise)
            + 0.0005 * regime
        )
        intraday = float(np.clip(predictable + rng.normal(0.0001, volatility), -0.12, 0.12))
        close_price = open_price * np.exp(intraday)
        excursion = abs(rng.normal(volatility * 0.7, volatility * 0.3))
        high_price = max(open_price, close_price) * np.exp(excursion)
        low_price = min(open_price, close_price) * np.exp(-excursion * rng.uniform(0.5, 1.2))
        volume_surprise = float(rng.normal(0, 0.35))
        volume = int(1_800_000 * np.exp(volume_surprise + abs(intraday) * 20))
        # 所有 OHLC 使用同一精度，保证舍入后仍满足高低价关系。
        history.append({
            "date": date,
            "open": round(open_price, 6),
            "high": round(high_price, 6),
            "low": round(low_price, 6),
            "close": round(close_price, 6),
            "volume": volume,
        })
        previous_close = close_price
        previous_intraday = intraday
        previous_volume_surprise = volume_surprise
    frame = pd.DataFrame(history)
    frame.attrs.update({"source": "synthetic", "symbol": symbol, "seed": settings["seed"]})
    return validate_frame(frame)


def parse_csv(raw: bytes) -> pd.DataFrame:
    """读取 UTF-8 / GB18030 CSV；不填补价格、不静默丢弃坏行。"""
    if not raw:
        raise ValueError("CSV 文件为空。")
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("CSV 文件不能超过 8 MB。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValueError("无法识别文件编码，请保存为 UTF-8 CSV。") from exc
    try:
        header = next(csv.reader(io.StringIO(text)))
        normalized = [name.strip().lower() for name in header]
        if len(normalized) != len(set(normalized)):
            raise ValueError("CSV 列名重复，请检查表头。")
        frame = pd.read_csv(io.StringIO(text), dtype=str, on_bad_lines="error")
        frame.columns = [str(name).strip().lower() for name in frame.columns]
    except (pd.errors.ParserError, pd.errors.EmptyDataError, StopIteration, csv.Error) as exc:
        raise ValueError("无法解析 CSV，请使用英文逗号分隔并包含表头。") from exc
    frame.attrs["source"] = "uploaded_csv"
    return validate_frame(frame)


def summarize(df: pd.DataFrame) -> dict:
    """供 API 和实验结果共用的数据范围说明。"""
    return {
        "rows": int(len(df)),
        "start": pd.Timestamp(df["date"].min()).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(df["date"].max()).strftime("%Y-%m-%d"),
    }


def canonical_csv(df: pd.DataFrame) -> bytes:
    """Stable UTF-8 / CRLF CSV for hashes on every OS, including legacy examples."""
    return df.to_csv(index=False, float_format="%.10g", lineterminator="\r\n").encode("utf-8")
