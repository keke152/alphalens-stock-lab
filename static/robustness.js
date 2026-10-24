"use strict";

function updateEvaluationControls() {
  $("folds-field").hidden = $("evaluation-select").value !== "walk_forward";
  const automatic = $("threshold-mode").value === "validation";
  $("threshold-caption").textContent = automatic ? "候选不足时的备用阈值" : "入场概率阈值";
  $("evaluation-help").textContent = $("evaluation-select").value === "walk_forward"
    ? "扩展训练窗口，每折重新训练；测试日期连续且不重复。"
    : "按时间约 60% / 20% / 20% 分配训练、验证与测试。";
  $("threshold-mode-help").textContent = automatic
    ? "仅用每折验证期净收益选择阈值；交易数不足时沿用下方备用值。测试集不参与选择。"
    : "整次实验采用下方固定阈值；候选分析仅供观察。";
}

function rowsHTML(rows) {
  return rows.map((row) => `<tr>${row.map((value) => `<td>${escapeHTML(value)}</td>`).join("")}</tr>`).join("");
}

function renderThresholdCandidates() {
  const fold = state.report?.evaluation?.folds?.find((item) => item.fold === Number($("threshold-fold").value));
  if (!fold) return;
  const analysis = fold.threshold_analysis;
  $("threshold-analysis-note").textContent = `${analysis.mode === "validation" ? "验证集选择" : "手动固定"} · 采用 ${percent(fold.threshold, 0)} · 验证期至少 ${analysis.minimum_trades} 笔交易${analysis.fallback ? " · 所有候选交易数不足，使用备用阈值" : ""}。候选排序不使用测试结果。`;
  $("threshold-candidates").innerHTML = analysis.candidates.map((row) => `<tr class="${row.threshold === fold.threshold ? "selected-candidate" : ""}"><td>${percent(row.threshold, 0)}${row.threshold === fold.threshold ? " · 采用" : ""}</td><td>${percent(row.total_return, 2, true)}</td><td>${percent(row.max_drawdown)}</td><td>${row.trades}</td><td>${row.eligible ? "满足" : "不足"}</td></tr>`).join("");
}

function renderRobustness() {
  const report = state.report, evaluation = report?.evaluation;
  $("robustness-legacy").hidden = Boolean(evaluation);
  $("robustness-results").hidden = !evaluation;
  if (!evaluation) return;
  $("evaluation-summary").textContent = `${evaluation.mode === "walk_forward" ? "扩展窗口" : "时间留出"} · ${evaluation.fold_count} 折 · ${evaluation.test_rows} 个样本外交易日`;
  $("evaluation-range").textContent = `${evaluation.test_start} → ${evaluation.test_end} · GAP ${evaluation.gap}`;
  const diagnostics = report.diagnostics;
  $("brier-score").textContent = number(diagnostics.brier_score, 4);
  $("observed-up-rate").textContent = percent(diagnostics.up_rate, 1);
  $("data-quality-summary").textContent = report.data_quality.warning_count
    ? `${report.data_quality.warning_count} 类提示需核对` : "结构校验通过 · 未触发提示项";
  $("fold-results").innerHTML = rowsHTML(evaluation.folds.map((fold) => {
    const split = fold.split;
    return [fold.fold, `${split.train.start} → ${split.train.end} (${split.train.rows})`,
      `${split.validation.start} → ${split.validation.end} (${split.validation.rows})`,
      `${split.test.target_start} → ${split.test.target_end} (${split.test.rows})`,
      percent(fold.threshold, 0), percent(fold.metrics.accuracy, 1), percent(fold.metrics.total_return, 2, true),
      percent(fold.metrics.max_drawdown)];
  }));
  const previous = Number($("threshold-fold").value);
  $("threshold-fold").innerHTML = evaluation.folds.map((fold) => `<option value="${fold.fold}">第 ${fold.fold} 折 · 验证期</option>`).join("");
  $("threshold-fold").value = evaluation.folds.some((fold) => fold.fold === previous) ? previous : evaluation.fold_count;
  renderThresholdCandidates();
  $("cost-scenarios").innerHTML = rowsHTML(report.cost_sensitivity.map((row) => [
    `${row.multiplier} ×${row.multiplier === 1 ? " · 当前" : ""}`, `${number(row.cost_bps, 1)} / ${number(row.slippage_bps, 1)}`,
    percent(row.total_return, 2, true), percent(row.max_drawdown), number(row.sharpe), row.trades]));
  $("quality-checks").innerHTML = report.data_quality.checks.map((check) => `<div class="quality-check"><span class="quality-status ${check.count ? "needs-review" : ""}">${check.count ? "!" : "✓"}</span><div><strong>${escapeHTML(check.label)} · ${check.count} 条</strong><p>${escapeHTML(check.detail)}</p></div></div>`).join("");
  $("reliability-bins").innerHTML = rowsHTML(diagnostics.reliability_bins.map((bin) => [
    `${percent(bin.lower, 0)} – ${percent(bin.upper, 0)}`, bin.count,
    percent(bin.mean_probability, 1), percent(bin.observed_up_rate, 1)]));
  const matrix = diagnostics.confusion;
  $("confusion-matrix").innerHTML = [["正确判断非上涨 TN", matrix.tn], ["误判为上涨 FP", matrix.fp],
    ["漏判上涨 FN", matrix.fn], ["正确判断上涨 TP", matrix.tp]].map(([label, count]) => `<div><small>${escapeHTML(label)}</small><strong>${number(count, 0)}</strong></div>`).join("");
}

function bindRobustnessControls() {
  for (const id of ["evaluation-select", "threshold-mode"]) $(id).addEventListener("change", updateEvaluationControls);
  $("threshold-fold").addEventListener("change", renderThresholdCandidates);
  updateEvaluationControls();
}
