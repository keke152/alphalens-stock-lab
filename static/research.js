/* Derived views of recorded experiments. No synthetic performance or live quotations. */
"use strict";
const research = { priceWindow: 90, comparisonCache: new Map(), comparisonSequence: 0,
  comparisonData: [], pairIds: null };

function chartColor(variable) {
  return getComputedStyle(document.documentElement).getPropertyValue(variable).trim();
}

function formConfig() {
  return { dataset_id: $("dataset-select").value, model: $("model-select").value,
    threshold: Number($("threshold").value), cost_bps: Number($("cost-bps").value),
    slippage_bps: Number($("slippage-bps").value), initial_capital: Number($("initial-capital-input").value),
    seed: Number($("seed-input").value) };
}

function showConfigDifference() {
  if (!state.report || state.busy) { $("config-dirty").hidden = true; return; }
  const original = state.report.config || state.run?.config || {};
  const current = formConfig();
  const dirty = Object.keys(original).some((key) => key in current && current[key] !== original[key]);
  $("config-dirty").hidden = !dirty;
  $("run-button-label").textContent = dirty ? "按新参数训练并回测" : "训练并回测";
}

function drawPriceChart() {
  if (!state.report || $("price-chart").closest("section").hidden) return;
  const all = state.report.prices || [];
  // Calculate trailing means before applying the display window; never use future prices.
  const points = all.map((row, index) => {
    const mean = (days) => index + 1 >= days
      ? all.slice(index - days + 1, index + 1).reduce((sum, item) => sum + item.close, 0) / days : null;
    return { ...row, ma5: mean(5), ma20: mean(20) };
  }).slice(-research.priceWindow);
  const series = [{ key: "close", label: "收盘价", color: chartColor("--teal"), fill: true }];
  if ($("show-ma").checked) series.push({ key: "ma5", label: "MA5", color: chartColor("--blue") },
    { key: "ma20", label: "MA20", color: chartColor("--amber"), dash: true });
  $("price-window").textContent = `最近 ${points.length} 日`;
  document.querySelector(".moving-average-legend").hidden = !$("show-ma").checked;
  lineChart("price-chart", points, series);
}

function resetPredictionFilters() {
  for (const id of ["filter-start", "filter-end"]) {
    $(id).value = "";
    const rows = state.report?.predictions || [];
    $(id).min = rows[0]?.date || "";
    $(id).max = rows[rows.length - 1]?.date || "";
  }
  $("filter-signal").value = "ALL";
  $("filter-correct").value = "ALL";
  state.page = 0;
}

function filteredPredictions() {
  const start = $("filter-start").value, end = $("filter-end").value;
  const signal = $("filter-signal").value, correct = $("filter-correct").value;
  return [...(state.report?.predictions || [])].filter((row) =>
    (!start || row.date >= start) && (!end || row.date <= end) &&
    (signal === "ALL" || row.signal === signal) &&
    (correct === "ALL" || row.correct === (correct === "correct"))).reverse();
}

function downloadFilteredCsv() {
  const rows = filteredPredictions();
  if (!rows.length) return;
  const fields = ["signal_date", "date", "probability_up", "predicted_return", "actual_return", "signal", "correct",
    "net_return", "benchmark_return", "strategy_equity", "benchmark_equity", "drawdown"];
  const equity = new Map((state.report.equity || []).map((row) => [row.date, row]));
  const csvCell = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const lines = [fields.join(","), ...rows.map((row) => {
    const day = equity.get(row.date) || {};
    const combined = { ...row, net_return: day.return, benchmark_return: day.benchmark_return,
      strategy_equity: day.strategy, benchmark_equity: day.benchmark, drawdown: day.drawdown };
    return fields.map((field) => csvCell(combined[field])).join(",");
  })];
  const blob = new Blob(["\uFEFF", lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob), link = document.createElement("a");
  link.href = url; link.download = `alphalens-${state.run.id}-filtered.csv`;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`已导出 ${rows.length} 条筛选记录，收益率字段使用小数。`);
}

function renderMonthlyReturns() {
  const groups = new Map();
  for (const day of state.report?.equity || []) {
    const month = day.date.slice(0, 7);
    if (!groups.has(month)) groups.set(month, { month, days: 0, strategy: 1, benchmark: 1, trades: 0 });
    const group = groups.get(month);
    group.days += 1; group.strategy *= 1 + day.return; group.benchmark *= 1 + day.benchmark_return;
    group.trades += Number(day.signal === "BUY");
  }
  const months = [...groups.values()];
  const profitable = months.filter((row) => row.strategy > 1).length;
  $("monthly-summary").textContent = `记录 ${months.length} 个月份 · 其中 ${profitable} 个月份策略收益为正 · 已扣双边费用及滑点`;
  $("monthly-body").innerHTML = months.reverse().map((row) => `<tr><td>${escapeHTML(row.month)}</td><td>${row.days}</td><td class="${signedClass(row.strategy - 1)}">${percent(row.strategy - 1, 2, true)}</td><td class="${signedClass(row.benchmark - 1)}">${percent(row.benchmark - 1, 2, true)}</td><td>${row.trades}</td></tr>`).join("");
}

function renderCompareOptions() {
  const completed = state.runs.filter((run) => run.status === "completed");
  const oldA = $("compare-a").value, oldB = $("compare-b").value;
  const fill = (id) => {
    const select = $(id); select.innerHTML = "";
    const empty = document.createElement("option"); empty.value = ""; empty.textContent = "选择一个已完成实验"; select.appendChild(empty);
    for (const run of completed) {
      const option = document.createElement("option"); option.value = run.id;
      option.textContent = `${modelLabel(run.config?.model)} · ${run.dataset_name} · ${run.id.startsWith("bundled") ? "随附示例" : run.id.slice(0, 7)}`;
      select.appendChild(option);
    }
  };
  fill("compare-a"); fill("compare-b");
  const contains = (id) => completed.some((run) => run.id === id);
  $("compare-a").value = contains(oldA) ? oldA : (completed.find((run) => run.id === state.run?.id)?.id || completed[0]?.id || "");
  const chosenA = completed.find((run) => run.id === $("compare-a").value);
  $("compare-b").value = contains(oldB) && oldB !== $("compare-a").value ? oldB :
    (completed.find((run) => run.id !== chosenA?.id && run.dataset_id === chosenA?.dataset_id && run.config?.model !== chosenA?.config?.model)?.id ||
      completed.find((run) => run.id !== chosenA?.id)?.id || "");
}

async function cachedRun(id) {
  if (research.comparisonCache.has(id)) return research.comparisonCache.get(id);
  const run = await api(`/api/runs/${encodeURIComponent(id)}`);
  if (run.status !== "completed" || !run.report) throw new Error("选中的实验尚未完成，请刷新实验列表。");
  research.comparisonCache.set(id, run);
  return run;
}

async function loadComparison() {
  const sequence = ++research.comparisonSequence;
  const aId = $("compare-a").value, bId = $("compare-b").value;
  $("comparison-result").hidden = true;
  research.comparisonData = [];
  $("compare-chart").innerHTML = "";
  if (!aId || !bId || aId === bId) {
    $("compare-message").textContent = "请选择两个不同的已完成实验，或点击右侧“一键双模型对比”生成新实验。";
    return;
  }
  $("compare-message").textContent = "正在读取两次实验的完整报告…";
  try {
    const [a, b] = await Promise.all([cachedRun(aId), cachedRun(bId)]);
    if (sequence !== research.comparisonSequence) return;
    const first = a.report, second = b.report;
    const sameData = first.dataset_sha256 && first.dataset_sha256 === second.dataset_sha256;
    const sameDates = first.equity.length === second.equity.length && first.equity.every((row, index) => row.date === second.equity[index].date);
    const configKeys = ["threshold", "cost_bps", "slippage_bps", "seed"];
    const changed = configKeys.filter((key) => first.config[key] !== second.config[key]);
    $("compare-a-title").textContent = `A · ${modelLabel(first.config.model)}`;
    $("compare-b-title").textContent = `B · ${modelLabel(second.config.model)}`;
    const items = [
      ["数据集", a.dataset_name, b.dataset_name],
      ["测试交易日", `${first.equity[0]?.date} → ${first.equity.at(-1)?.date}`, `${second.equity[0]?.date} → ${second.equity.at(-1)?.date}`],
      ["样本外准确率", percent(first.metrics.accuracy, 1), percent(second.metrics.accuracy, 1)],
      ["AUC", number(first.metrics.auc, 3), number(second.metrics.auc, 3)],
      ["策略累计收益", percent(first.metrics.total_return, 2, true), percent(second.metrics.total_return, 2, true)],
      ["最大回撤", percent(first.metrics.max_drawdown), percent(second.metrics.max_drawdown)],
      ["年化夏普", number(first.metrics.sharpe), number(second.metrics.sharpe)],
      ["模拟交易次数", number(first.metrics.trades, 0), number(second.metrics.trades, 0)],
      ["初始资金", number(first.config.initial_capital, 0), number(second.config.initial_capital, 0)],
      ["入场阈值", percent(first.config.threshold, 0), percent(second.config.threshold, 0)],
      ["单边手续费 / 滑点", `${first.config.cost_bps} / ${first.config.slippage_bps} bps`, `${second.config.cost_bps} / ${second.config.slippage_bps} bps`],
      ["模型种子", first.config.seed, second.config.seed]
    ];
    $("compare-metrics").innerHTML = items.map((item) => `<tr>${item.map((cell) => `<td>${escapeHTML(cell)}</td>`).join("")}</tr>`).join("");
    $("comparison-result").hidden = false;
    if (sameData && sameDates) {
      $("compare-message").textContent = `同一份数据、相同测试日期。${changed.length ? "两次实验的部分参数不同，请结合下表解释差异。" : "阈值、交易成本与随机种子一致，可以观察模型选择带来的结果差异。"} 默认样本为合成行情，历史表现不能保证未来结果。`;
      research.comparisonData = [{ date: first.predictions[0]?.signal_date || "起点", a: 1, b: 1 },
        ...first.equity.map((row, index) => ({ date: row.date, a: row.strategy / first.config.initial_capital,
          b: second.equity[index].strategy / second.config.initial_capital }))];
      requestAnimationFrame(drawComparisonChart);
    } else {
      $("compare-message").textContent = "数据指纹或测试日期不同：下表仅用于并列阅读，不能据此判断哪个模型更好；已停止绘制叠加曲线。";
      $("compare-chart").innerHTML = '<p class="chart-empty">请选取同一份数据、相同测试日期的实验。</p>';
    }
  } catch (error) {
    if (sequence === research.comparisonSequence) $("compare-message").textContent = error.message;
  }
}

function drawComparisonChart() {
  if (state.view !== "compare" || !research.comparisonData.length || $("comparison-result").hidden) return;
  lineChart("compare-chart", research.comparisonData, [
    { key: "a", label: "实验 A 净值", color: chartColor("--teal"), fill: true },
    { key: "b", label: "实验 B 净值", color: chartColor("--blue"), dash: true }
  ]);
}

async function submitModelPair() {
  if (state.busy || !$("run-form").reportValidity()) return;
  const configuration = formConfig();
  const ids = [];
  research.pairIds = ids;
  setBusy(true); showError("");
  try {
    for (const model of ["random_forest", "logistic"]) {
      const run = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...configuration, model }) });
      ids.push(run.id);
    }
    research.pairIds = ids;
    await refreshHistory().catch(() => {});
    await openRun(ids[1], true);
    toast("已按相同数据和参数提交两个模型，完成后可查看对比。");
    // Fast machines may finish both jobs before the first polling round.
    if (state.run?.status === "completed") await finishPairIfReady(ids[1]);
  } catch (error) {
    research.pairIds = null;
    setBusy(false);
    if (ids.length) {
      await refreshHistory().catch(() => {});
      await openRun(ids[0], true);
    }
    showError(`${error.message}${ids.length ? " 第一组任务已提交，可在实验记录查看，稍后补充另一模型。" : ""}`);
  }
}

async function finishPairIfReady(completedId) {
  if (!research.pairIds || research.pairIds[1] !== completedId) return;
  const ids = research.pairIds;
  const first = await cachedRun(ids[0]).catch(() => null);
  if (!first) { toast("其中一组模型未成功完成，请查看实验记录。"); return; }
  await refreshHistory().catch(() => {
    if (!state.runs.some((run) => run.id === first.id)) state.runs.unshift(first);
    if (state.run?.id === completedId && !state.runs.some((run) => run.id === completedId)) state.runs.unshift(state.run);
    renderHistory();
  });
  research.pairIds = null;
  renderCompareOptions();
  $("compare-a").value = ids[0]; $("compare-b").value = ids[1];
  setView("compare");
}

function bindResearchControls() {
  document.querySelectorAll("[data-window]").forEach((button) => button.addEventListener("click", () => {
    research.priceWindow = Number(button.dataset.window);
    document.querySelectorAll("[data-window]").forEach((item) => {
      const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-pressed", String(active));
    });
    drawPriceChart();
  }));
  $("show-ma").addEventListener("change", drawPriceChart);
  for (const id of ["filter-start", "filter-end", "filter-signal", "filter-correct"]) {
    $(id).addEventListener("change", () => { state.page = 0; renderPredictions(); });
  }
  $("reset-filters").addEventListener("click", () => { resetPredictionFilters(); renderPredictions(); });
  $("export-filtered").addEventListener("click", downloadFilteredCsv);
  $("compare-a").addEventListener("change", loadComparison);
  $("compare-b").addEventListener("change", loadComparison);
  $("compare-run-button").addEventListener("click", submitModelPair);
  $("run-form").addEventListener("input", showConfigDifference);
  $("run-form").addEventListener("change", showConfigDifference);
}
