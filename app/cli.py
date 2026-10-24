"""无需浏览器也能生成数据、训练并保存完整研究报告。"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .data import generate_demo, parse_csv
from .engine import run_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description="AlphaLens 本地股票研究工具")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate-demo", help="生成可复现的合成 OHLCV 数据")
    generate.add_argument("--symbol", choices=["DEMO.A", "DEMO.B", "DEMO.C"], default="DEMO.A")
    generate.add_argument("--rows", type=int, default=1000)
    generate.add_argument("--output", type=Path, required=True)
    train = sub.add_parser("train", help="按时间顺序训练并生成回测报告")
    train.add_argument("--csv", type=Path, required=True)
    train.add_argument("--output", type=Path, required=True)
    train.add_argument("--save-model", type=Path, default=None)
    train.add_argument("--model", choices=["random_forest", "logistic"], default="random_forest")
    train.add_argument("--threshold", type=float, default=0.55)
    train.add_argument("--cost-bps", type=float, default=5)
    train.add_argument("--slippage-bps", type=float, default=2)
    train.add_argument("--initial-capital", type=float, default=100000)
    train.add_argument("--seed", type=int, default=42)
    train.add_argument("--source", choices=["synthetic", "uploaded"], default="uploaded")
    train.add_argument("--name", default="命令行研究")
    train.add_argument("--evaluation", choices=["holdout", "walk_forward"], default="holdout")
    train.add_argument("--folds", type=int, choices=[3, 4, 5], default=3)
    train.add_argument("--threshold-mode", choices=["manual", "validation"], default="manual")
    train.add_argument("--html", type=Path, help="同时保存可离线打开、打印的 HTML 研究报告")
    args = parser.parse_args()
    try:
        if args.command == "generate-demo":
            if not 300 <= args.rows <= 20000:
                parser.error("rows 需要在 300 到 20000 之间")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            generate_demo(args.symbol, args.rows).to_csv(args.output, index=False, float_format="%.10g")
            print(f"Saved synthetic data: {args.output.resolve()}")
            return
        from .main import RunRequest
        config = RunRequest(dataset_id="cli", model=args.model, threshold=args.threshold,
                            cost_bps=args.cost_bps, slippage_bps=args.slippage_bps,
                            initial_capital=args.initial_capital, seed=args.seed,
                            evaluation=args.evaluation, folds=args.folds,
                            threshold_mode=args.threshold_mode).model_dump(exclude={"dataset_id"})
        df = parse_csv(args.csv.read_bytes())
        df.attrs["source"] = args.source
        report = run_experiment(df, config, artifact_path=args.save_model)
        canonical = df.to_csv(index=False, float_format="%.10g").encode("utf-8")
        report.update(dataset_name=args.name, dataset_source=args.source,
                      dataset_sha256=hashlib.sha256(canonical).hexdigest(),
                      generated_at=datetime.now(timezone.utc).isoformat(), app_version=__version__)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        if args.html:
            from .reporting import render_html_report
            args.html.parent.mkdir(parents=True, exist_ok=True)
            args.html.write_text(render_html_report(report, "cli"), encoding="utf-8")
        print(json.dumps({"report": str(args.output.resolve()), "metrics": report["metrics"]}, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
