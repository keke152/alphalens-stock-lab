"""HTTP 接口、任务队列和本地静态界面。默认只在本机运行。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, storage
from .data import generate_demo, parse_csv
from .engine import DEFAULT_CONFIG, run_experiment
from .reporting import render_html_report

LOGGER = logging.getLogger("alphalens")
MAX_UPLOAD = 8 * 1024 * 1024
DEFAULTS = DEFAULT_CONFIG.copy()
SUBMIT_LOCK = threading.Lock()


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    dataset_id: str = Field(min_length=1, max_length=100)
    model: Literal["random_forest", "logistic"] = "random_forest"
    threshold: float = Field(default=0.55, ge=0.5, le=0.9)
    cost_bps: float = Field(default=5, ge=0, le=100)
    slippage_bps: float = Field(default=2, ge=0, le=100)
    initial_capital: float = Field(default=100000, ge=1000, le=1_000_000_000)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    evaluation: Literal["holdout", "walk_forward"] = "holdout"
    folds: int = Field(default=3, ge=3, le=5)
    threshold_mode: Literal["manual", "validation"] = "manual"


def seed_data() -> None:
    for code, symbol, name in (("demo-a", "DEMO.A", "星河科技 · 合成行情"),
                               ("demo-b", "DEMO.B", "远山能源 · 合成行情"),
                               ("demo-c", "DEMO.C", "海蓝消费 · 合成行情")):
        if not storage.get_dataset(code):
            bundled = storage.ROOT / "data" / f"{code}.csv"
            frame = parse_csv(bundled.read_bytes()) if bundled.exists() else generate_demo(symbol)
            storage.register_dataset(code, name, symbol, "synthetic", frame)
    # 随源码提供的报告由真实模型生成；首次启动可直接查看，点击训练会生成独立任务。
    for run_id, filename in (("bundled-logistic", "logistic-report.json"), ("bundled-demo", "demo-report.json")):
        example = storage.ROOT / "examples" / filename
        if not example.exists() or storage.get_run(run_id):
            continue
        report = json.loads(example.read_text(encoding="utf-8"))
        dataset = storage.get_dataset("demo-a")
        if report.get("dataset_sha256") == dataset["sha256"]:
            target = storage.HOME / "reports" / f"{run_id}.json"
            target.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            storage.add_run(run_id, dataset, report.get("config", DEFAULTS), "completed")
            storage.set_run(run_id, report_path=str(target), finished_at=storage.now())


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.initialize()
    seed_data()
    app.state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")
    yield
    app.state.executor.shutdown(wait=True, cancel_futures=True)


app = FastAPI(title="AlphaLens AI 股票研究室", version=__version__, lifespan=lifespan,
              description="本地研究演示 API：CSV 导入、时序训练、方向预测、含成本回测与报告导出。")
hosts = os.environ.get("STOCK_LAB_ALLOWED_HOSTS", "127.0.0.1,localhost,::1").split(",")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host.strip() for host in hosts])


@app.middleware("http")
async def local_protection(request: Request, call_next):
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme != request.url.scheme:
                return JSONResponse({"detail": "请从本机项目页面提交请求。"}, status_code=403)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_UPLOAD + 65536:
                    return JSONResponse({"detail": "文件不能超过 8 MB。"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "请求长度无效。"}, status_code=400)
    response = await call_next(request)
    # Windows registry may map .js to text/plain. Keep nosniff enabled and send
    # the correct MIME type explicitly for the scripts we serve locally.
    if request.url.path.startswith("/static/") and response.status_code in {200, 304}:
        if request.url.path.endswith(".js"):
            response.headers["Content-Type"] = "text/javascript; charset=utf-8"
        elif request.url.path.endswith(".css"):
            response.headers["Content-Type"] = "text/css; charset=utf-8"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/bootstrap")
def bootstrap():
    return {"datasets": storage.list_datasets(), "defaults": DEFAULTS, "runs": storage.list_runs(),
            "limits": {"max_upload_mb": 8, "min_rows": 300, "max_rows": 20000, "max_pending_runs": 5}}


@app.get("/api/datasets")
def datasets():
    return storage.list_datasets()


@app.post("/api/datasets/upload", status_code=201)
async def upload_dataset(file: UploadFile = File(...), name: str = Form("")):
    raw = await file.read(MAX_UPLOAD + 1)
    await file.close()
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "文件不能超过 8 MB。")
    try:
        df = parse_csv(raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    canonical = df.to_csv(index=False, float_format="%.10g").encode("utf-8")
    dataset_id = "csv-" + hashlib.sha256(canonical).hexdigest()[:20]
    existing = storage.get_dataset(dataset_id)
    if existing:
        return storage.public_dataset(existing)
    if len(storage.list_datasets()) >= 50:
        raise HTTPException(409, "当前演示最多保存 50 份数据集，请使用新的 STOCK_LAB_HOME 工作目录。")
    display_name = (name.strip() or Path(file.filename or "用户行情.csv").stem).replace("\x00", "")[:80]
    return storage.register_dataset(dataset_id, display_name, "CSV", "uploaded", df)


@app.get("/api/datasets/{dataset_id}/download")
def download_data(dataset_id: str):
    dataset = storage.get_dataset(dataset_id)
    if not dataset:
        raise HTTPException(404, "数据集不存在。")
    return FileResponse(dataset["path"], media_type="text/csv", filename=f"{dataset_id}.csv")


def execute_run(run_id: str, dataset: dict, config: dict) -> None:
    try:
        storage.set_run(run_id, status="running")
        df = parse_csv(Path(dataset["path"]).read_bytes())
        df.attrs["source"] = dataset["source"]
        model_path = storage.HOME / "models" / f"{run_id}.joblib"
        report = run_experiment(df, config, artifact_path=model_path)
        report.update(dataset_id=dataset["id"], dataset_name=dataset["name"],
                      dataset_source=dataset["source"], dataset_sha256=dataset["sha256"],
                      generated_at=storage.now(), app_version=__version__)
        path = storage.HOME / "reports" / f"{run_id}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        storage.set_run(run_id, status="completed", report_path=str(path), model_path=str(model_path), finished_at=storage.now())
    except Exception as exc:
        LOGGER.exception("研究任务失败 %s", run_id)
        message = str(exc) if isinstance(exc, ValueError) else "计算未完成，请查看服务终端日志后重试。"
        storage.set_run(run_id, status="failed", error=message, finished_at=storage.now())


@app.post("/api/runs", status_code=202)
def create_run(body: RunRequest):
    dataset = storage.get_dataset(body.dataset_id)
    if not dataset:
        raise HTTPException(404, "请先选择或导入数据集。")
    config = body.model_dump(exclude={"dataset_id"})
    with SUBMIT_LOCK:
        if storage.pending_count() >= 5:
            raise HTTPException(429, "已有 5 个研究任务，请等待当前任务完成。")
        run_id = uuid.uuid4().hex
        storage.add_run(run_id, dataset, config)
        app.state.executor.submit(execute_run, run_id, dataset, config)
    return {"id": run_id, "status": "queued"}


@app.get("/api/runs")
def runs():
    return storage.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "研究任务不存在。")
    result = storage.public_run(run)
    if run["status"] == "completed":
        result["report"] = json.loads(Path(run["report_path"]).read_text(encoding="utf-8"))
    return result


def completed_run(run_id: str) -> dict:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "研究任务不存在。")
    if run["status"] != "completed":
        raise HTTPException(409, "任务尚未完成。")
    return run


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str, format: Literal["json", "csv", "html"] = "json"):
    run = completed_run(run_id)
    if format == "json":
        return FileResponse(run["report_path"], media_type="application/json", filename=f"alphalens-{run_id}.json")
    report = json.loads(Path(run["report_path"]).read_text(encoding="utf-8"))
    if format == "html":
        return Response(render_html_report(report, run_id), media_type="text/html",
                        headers={"Content-Disposition": f'attachment; filename="alphalens-{run_id}.html"'})
    buffer = io.StringIO(newline="")
    fields = ["signal_date", "date", "fold", "threshold", "probability_up", "predicted_return", "actual_return", "signal", "correct",
              "net_return", "benchmark_return", "strategy_equity", "benchmark_equity", "drawdown"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    equity = {row["date"]: row for row in report["equity"]}
    for prediction in report["predictions"]:
        day = equity.get(prediction["date"], {})
        row = dict(prediction, net_return=day.get("return"), benchmark_return=day.get("benchmark_return"),
                   strategy_equity=day.get("strategy"), benchmark_equity=day.get("benchmark"), drawdown=day.get("drawdown"))
        writer.writerow(row)
    return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="alphalens-{run_id}.csv"'})


@app.get("/api/runs/{run_id}/model")
def download_model(run_id: str):
    run = completed_run(run_id)
    if not run["model_path"]:
        raise HTTPException(404, "随附报告未包含模型文件，点击「开始研究」训练后即可导出。")
    return FileResponse(run["model_path"], filename=f"alphalens-{run_id}.joblib", media_type="application/octet-stream")


def document_list() -> list[dict]:
    items = [{"name": "README.md", "title": "项目说明与快速启动", "url": "/api/documents/README.md"}]
    for path in sorted((storage.ROOT / "docs").glob("*.md")):
        first = path.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
        items.append({"name": path.name, "title": first, "url": f"/api/documents/{path.name}"})
    return items


@app.get("/api/documents")
def documents():
    return {"documents": document_list()}


@app.get("/api/documents/{name}")
def document(name: str):
    if name not in {row["name"] for row in document_list()}:
        raise HTTPException(404, "文档不存在。")
    path = storage.ROOT / name if name == "README.md" else storage.ROOT / "docs" / name
    return FileResponse(path, media_type="text/plain; charset=utf-8")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=storage.ROOT / "static"), name="assets")
app.mount("/", StaticFiles(directory=storage.ROOT / "static", html=True), name="interface")
