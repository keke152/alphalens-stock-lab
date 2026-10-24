"""Standalone, escaped HTML reports; no remote assets or script dependencies."""
from __future__ import annotations

from html import escape
import math


def text(value) -> str:
    return escape(str(value if value is not None else "—"), quote=True)


def number(value, percent=False) -> str:
    if not isinstance(value, (float, int)) or not math.isfinite(value):
        return "—"
    return f"{value * 100:.2f}%" if percent else f"{value:,.3f}"


def table(headers, rows) -> str:
    return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{text(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{text(cell)}</td>' for cell in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def equity_chart(report: dict) -> str:
    equity = report.get("equity", [])
    if not equity:
        return ""
    # Limit SVG size for long imported histories; the JSON keeps all daily values.
    step = max(1, math.ceil(len(equity) / 500))
    rows = equity[::step]
    if rows[-1] is not equity[-1]:
        rows.append(equity[-1])
    capital = report["config"]["initial_capital"]
    values = [[1] + [row[key] / capital for row in rows] for key in ("strategy", "benchmark")]
    low, high = min(min(v) for v in values), max(max(v) for v in values)
    span = max(high - low, .01)
    lines = []
    for data, color in zip(values, ("#008577", "#6685b1")):
        points = ' '.join(f'{30 + i / max(1, len(data)-1) * 840:.1f},{220 - (value-low)/span*190:.1f}' for i, value in enumerate(data))
        lines.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
    return '<svg viewBox="0 0 900 255" role="img" aria-label="策略与日内基准净值">' + ''.join(lines) + f'<text x="30" y="247" fill="#51677f" font-size="12">策略 / 日内基准 · 净值从 1 起 · {text(equity[0]["date"])} — {text(equity[-1]["date"])}</text></svg>'


def render_html_report(report: dict, run_id: str = "") -> str:
    config, metrics = report.get("config", {}), report.get("metrics", {})
    evaluation = report.get("evaluation", {})
    folds = evaluation.get("folds", [])
    mode = "扩展窗口滚动评估" if config.get("evaluation") == "walk_forward" else "单次时间留出"
    cards = ''.join(f'<article><small>{text(label)}</small><strong>{number(metrics.get(key), is_percent)}</strong></article>'
                    for label, key, is_percent in [("样本外准确率", "accuracy", True), ("策略收益", "total_return", True),
                                                  ("最大回撤", "max_drawdown", True), ("年化夏普", "sharpe", False)])
    split_rows = []
    for fold in folds or [{"fold": 1, "split": report.get("split", {}), "threshold": config.get("threshold"), "metrics": metrics}]:
        split = fold["split"]
        split_rows.append([fold["fold"], f'{split.get("train", {}).get("start")} → {split.get("train", {}).get("end")}',
                           f'{split.get("validation", {}).get("start")} → {split.get("validation", {}).get("end")}',
                           f'{split.get("test", {}).get("target_start")} → {split.get("test", {}).get("target_end")}',
                           number(fold["threshold"], True), number(fold["metrics"].get("total_return"), True)])
    costs = table(["费用倍数", "手续费 / 滑点 (bps)", "策略收益", "最大回撤", "交易数"], [
        [row["multiplier"], f'{row["cost_bps"]} / {row["slippage_bps"]}', number(row["total_return"], True),
         number(row["max_drawdown"], True), row["trades"]] for row in report.get("cost_sensitivity", [])])
    threshold_sections = ''
    for fold in folds:
        analysis = fold.get("threshold_analysis", {})
        threshold_sections += f'<h3>第 {fold["fold"]} 折 · 采用 {number(fold["threshold"], True)}</h3>'
        threshold_sections += table(["候选阈值", "验证期净收益", "验证期交易数", "满足交易数要求"], [
            [number(row["threshold"], True), number(row["total_return"], True), row["trades"], "是" if row["eligible"] else "否"]
            for row in analysis.get("candidates", [])])
    quality = table(["数据检查", "记录数", "说明"], [[row["label"], row["count"], row["detail"]]
                         for row in report.get("data_quality", {}).get("checks", [])])
    diagnostics = report.get("diagnostics", {})
    calibration = table(["分数区间", "样本数", "平均上涨分数", "实际上涨比例"], [
        [f'{number(row["lower"], True)} — {number(row["upper"], True)}', row["count"],
         number(row["mean_probability"], True), number(row["observed_up_rate"], True)]
        for row in diagnostics.get("reliability_bins", [])])
    predictions = table(["信号日", "交易日", "折", "阈值", "上涨分数", "实际收益", "信号"], [
        [row["signal_date"], row["date"], row.get("fold", 1), number(row.get("threshold", config.get("threshold")), True),
         number(row["probability_up"], True), number(row["actual_return"], True), row["signal"]]
        for row in report.get("predictions", [])[-30:]])
    notes = ''.join(f'<li>{text(note)}</li>' for note in report.get("notes", []))
    parameters = table(["参数", "值"], list(config.items()))
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaLens 研究报告 · {text(report.get("dataset_name", ""))}</title>
<style>body{{margin:0;background:#edf3f8;color:#203b58;font:14px/1.7 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1060px;margin:40px auto;padding:40px;background:white;border-radius:16px}}h1{{font-size:32px;margin:5px 0}}h2{{margin-top:32px}}h3{{font-size:15px}}.eyebrow{{color:#008577;letter-spacing:3px;font-size:12px}}.muted,small{{color:#637a91}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0}}article{{padding:18px;background:#f3f8fb;border:1px solid #dce7f0;border-radius:10px}}article strong{{display:block;font-size:25px}}table{{border-collapse:collapse;width:100%;font-size:12px}}td,th{{border-bottom:1px solid #dce7f0;text-align:left;padding:10px;vertical-align:top}}th{{background:#f3f8fb}}.scroll{{overflow:auto}}code{{word-break:break-all}}svg{{width:100%;height:auto}}footer{{border-top:1px solid #dce7f0;margin-top:28px;padding-top:20px}}@media(max-width:720px){{main{{margin:0;padding:20px}}.cards{{grid-template-columns:repeat(2,1fr)}}}}@media print{{body{{background:white}}main{{margin:0;padding:0;max-width:none}}article,tr{{break-inside:avoid}}h2,h3{{break-after:avoid}}.scroll{{overflow:visible}}}}</style></head>
<body><main><div class="eyebrow">ALPHALENS / RESEARCH NOTE</div><h1>{text(report.get("dataset_name", "股票趋势研究报告"))}</h1>
<p class="muted">{text(mode)} · {text(report.get("model", {}).get("name"))} · {text(report.get("dataset_source", "未知来源"))}</p>
<p>目标：下一条行情记录开盘至收盘的方向与收益。生成时间：{text(report.get("generated_at"))}</p>
<div class="cards">{cards}</div><h2>样本外净值</h2>{equity_chart(report)}
<h2>时间边界与每折结果</h2><p>训练与验证、验证与测试之间各隔离一行。滚动评估净值跨折延续；逐折收益各自从 1 起计算。</p>
{table(["折", "训练信号日期", "验证信号日期", "测试交易日期", "采用阈值", "该折收益"], split_rows)}
<h2>验证期阈值分析</h2><p>候选仅使用验证期结果。自动模式按含成本验证收益选择，最低交易数要求见 JSON；手动模式保留配置阈值。无候选满足要求时沿用手动阈值。</p>{threshold_sections or '<p>此旧版报告未记录候选分析。</p>'}
<h2>成本敏感性</h2><p>固定原预测、阈值和交易信号，单独改变手续费与滑点；不自动选择测试期最优参数。</p>{costs}
<h2>概率诊断</h2><p>Brier 分数：{number(diagnostics.get("brier_score"))}。以下分箱为描述性检查，模型输出尚未校准。</p>{calibration}
<h2>数据质量提示</h2>{quality}<p>仅作描述性检查，不自动删除或填补记录，不根据这些检查结果选择参数。</p>
<h2>最近 30 条样本外预测</h2>{predictions}<p>完整逐日记录保存在 JSON / CSV 导出中。</p>
<h2>实验参数</h2>{parameters}<h2>研究假设</h2><ul>{notes}</ul>
<footer><p>实验 ID：{text(run_id)} · 应用版本：{text(report.get("app_version"))}</p><p>数据 SHA256：<code>{text(report.get("dataset_sha256"))}</code></p><p class="muted">独立 HTML，可离线打开；使用浏览器打印功能保存为 PDF。</p></footer></main></body></html>'''
