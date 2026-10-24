"""每次操作使用独立 SQLite 连接，文件名仅来自内部生成的 ID。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_default_home = Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Documents' / 'AlphaLensStockLab' / 'user-data' if getattr(sys, 'frozen', False) else ROOT / 'runtime'
HOME = Path(os.environ.get("STOCK_LAB_HOME", str(_default_home))).resolve()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(HOME / "research.sqlite3", timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    for folder in (HOME, HOME / "datasets", HOME / "reports", HOME / "models"):
        folder.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, symbol TEXT NOT NULL,
                source TEXT NOT NULL, rows INTEGER NOT NULL, start TEXT NOT NULL,
                end TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, dataset_name TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
                config TEXT NOT NULL, report_path TEXT, model_path TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                dataset_id TEXT PRIMARY KEY, cost_price REAL, cost_date TEXT,
                rules TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, dataset_name TEXT NOT NULL,
                as_of TEXT NOT NULL, rule TEXT NOT NULL, title TEXT NOT NULL,
                detail TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_trades (
                id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, side TEXT NOT NULL,
                quantity INTEGER NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
                trade_date TEXT NOT NULL, realized_pnl REAL NOT NULL, created_at TEXT NOT NULL
            );
        """)
        db.execute("UPDATE runs SET status='failed', error=?, finished_at=? WHERE status IN ('running','queued')",
                   ("服务曾中断，任务未完成。请重新运行研究。", now()))


def public_dataset(row: sqlite3.Row | dict) -> dict:
    return {key: row[key] for key in ("id", "name", "symbol", "source", "rows", "start", "end", "sha256", "created_at")}


def list_datasets() -> list[dict]:
    with connect() as db:
        return [public_dataset(row) for row in db.execute("SELECT * FROM datasets ORDER BY source DESC, created_at DESC")]


def get_dataset(dataset_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    return dict(row) if row else None


def register_dataset(dataset_id: str, name: str, symbol: str, source: str, df: Any) -> dict:
    from .data import canonical_csv, summarize
    raw = canonical_csv(df)
    info = summarize(df)
    path = HOME / "datasets" / f"{dataset_id}.csv"
    path.write_bytes(raw)
    with connect() as db:
        db.execute("""INSERT OR IGNORE INTO datasets
            (id,name,symbol,source,rows,start,end,path,sha256,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (dataset_id, name, symbol, source, int(info["rows"]),
            str(info["start"]), str(info["end"]), str(path), hashlib.sha256(raw).hexdigest(), now()))
    return public_dataset(get_dataset(dataset_id))


def add_run(run_id: str, dataset: dict, config: dict, status: str = "queued") -> None:
    with connect() as db:
        db.execute("INSERT INTO runs (id,dataset_id,dataset_name,status,created_at,config) VALUES (?,?,?,?,?,?)",
                   (run_id, dataset["id"], dataset["name"], status, now(), json.dumps(config)))


def set_run(run_id: str, **updates: Any) -> None:
    allowed = {"status", "finished_at", "report_path", "model_path", "error"}
    if not updates or not set(updates).issubset(allowed):
        raise ValueError("不支持的任务字段")
    fields = ", ".join(f"{key}=?" for key in updates)
    with connect() as db:
        db.execute(f"UPDATE runs SET {fields} WHERE id=?", (*updates.values(), run_id))


def public_run(row: dict | sqlite3.Row) -> dict:
    result = {key: row[key] for key in ("id", "dataset_id", "dataset_name", "status", "created_at", "finished_at", "error")}
    result["config"] = json.loads(row["config"])
    result["has_model"] = bool(row["model_path"])
    return result


def list_runs() -> list[dict]:
    with connect() as db:
        return [public_run(row) for row in db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 100")]


def get_run(run_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def pending_count() -> int:
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]


def get_setting(key: str, default: dict) -> dict:
    with connect() as db:
        row = db.execute('SELECT value FROM workspace_settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default.copy()


def set_setting(key: str, value: dict) -> None:
    with connect() as db:
        db.execute('INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(value, ensure_ascii=False, allow_nan=False)))
