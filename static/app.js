/* AlphaLens: local, dependency-free dashboard. Values are always read from real API reports. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { datasets: [], runs: [], report: null, run: null, view: "overview", page: 0, pageSize: 8,
  pollTimer: null, clockTimer: null, selection: 0, busy: false, limits: {}, charts: [], toastTimer: null };
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const number = (value, digits = 2) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";
const percent = (value, digits = 2, sign = false) => typeof value === "number" && Number.isFinite(value) ? `${sign && value > 0 ? "+" : ""}${number(value * 100, digits)}%` : "—";
const signedClass = (value) => typeof value === "number" ? value > 0 ? "positive" : value < 0 ? "negative" : "" : "";
const shortDate = (value) => value ? String(value).slice(0, 10) : "—";
const modelLabel = (value) => value === "logistic" ? "逻辑回归" : "随机森林";

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, cache: "no-store" });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      let message = body?.detail || `请求失败（HTTP ${response.status}）`;
      if (Array.isArray(message)) message = message.map((item) => `${item.loc?.slice(1).join(".") || "参数"}：${item.msg || "不符合要求"}`).join("\n");
      throw new Error(typeof message === "string" ? message : JSON.stringify(message));
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时。请确认本地服务仍在运行，再从实验记录重新打开。");
    if (error instanceof TypeError) throw new Error("无法连接本地服务。请确认启动窗口保持打开，并刷新页面重试。");
    throw error;
  } finally { clearTimeout(timeout); }
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4000);
}

function showError(message) {
  $("error-banner").textContent = message || "";
  $("error-banner").hidden = !message;
}

function setBusy(busy) {
  state.busy = busy;
  $("run-button").disabled = busy || !$("dataset-select").value;
  $("run-button-label").textContent = busy ? "实验运行中…" : "训练并回测";
  $("run-button").setAttribute("aria-busy", String(busy));
  $("run-status").hidden = !busy;
  for (const field of $("run-form").querySelectorAll("input, select")) field.disabled = busy;
  $("upload-button").disabled = busy;
  $("compare-run-button").disabled = busy || !$("dataset-select").value;
}

function stopWatching() {
  clearTimeout(state.pollTimer);
  clearInterval(state.clockTimer);
  state.pollTimer = null;
  state.clockTimer = null;
}

function setDownload(id, url) {
  const link = $(id);
  if (url) { link.href = url; link.removeAttribute("aria-disabled"); }
  else { link.removeAttribute("href"); link.setAttribute("aria-disabled", "true"); }
}

function updateDataset() {
  syncToolDatasets();
  const dataset = state.datasets.find((item) => item.id === $("dataset-select").value);
  if (!dataset) return;
  $("dataset-name").textContent = dataset.name;
  $("dataset-symbol").textContent = dataset.symbol;
  $("dataset-source").textContent = dataset.source === "synthetic" ? "合成演示" : "CSV 导入";
  $("dataset-source").classList.toggle("uploaded", dataset.source !== "synthetic");
  $("dataset-range").textContent = `${shortDate(dataset.start)} — ${shortDate(dataset.end)}`;
  $("dataset-rows").textContent = `${number(dataset.rows, 0)} 个行情记录日`;
  $("data-config-source").textContent = dataset.source === "synthetic" ? "程序生成 · 虚构证券" : "用户上传 · 本地保存";
  $("source-notice").textContent = dataset.source === "synthetic" ? "当前为合成行情，非真实证券。" : "当前使用用户导入的行情。";
  setDownload("download-dataset", `/api/datasets/${encodeURIComponent(dataset.id)}/download`);
  if (!state.busy) { $("run-button").disabled = false; $("compare-run-button").disabled = false; }
}

function renderDatasets(selected) {
  const select = $("dataset-select");
  select.innerHTML = "";
  for (const dataset of state.datasets) {
    const option = document.createElement("option");
    option.value = dataset.id;
    option.textContent = dataset.name;
    select.appendChild(option);
  }
  if (state.datasets.some((item) => item.id === selected)) select.value = selected;
  updateDataset();
}

function updateThreshold() {
  const input = $("threshold");
  $("threshold-value").textContent = percent(Number(input.value), 0);
  const progress = (Number(input.value) - Number(input.min)) / (Number(input.max) - Number(input.min)) * 100;
  input.style.backgroundImage = `linear-gradient(to right,var(--teal) 0%,var(--teal) ${progress}%,var(--border) ${progress}%,var(--border) 100%)`;
  input.setAttribute("aria-valuetext", `${Math.round(Number(input.value) * 100)}%`);
}

function updateModelHelp() {
  $("model-help").textContent = $("model-select").value === "logistic"
    ? "标准化后进行线性方向分类，并用 Ridge 回归估计收益，作为可解释的基线。"
    : "以多棵决策树学习历史特征与涨跌的非线性关系，同时拟合收益回归器。";
}

function applyConfig(config = {}) {
  config = { evaluation: "holdout", folds: 3, threshold_mode: "manual", ...config };
  const fields = { model: "model-select", threshold: "threshold", cost_bps: "cost-bps", slippage_bps: "slippage-bps", initial_capital: "initial-capital-input", seed: "seed-input", evaluation: "evaluation-select", folds: "folds-input", threshold_mode: "threshold-mode" };
  for (const [key, id] of Object.entries(fields)) if (config[key] !== undefined && config[key] !== null) $(id).value = config[key];
  updateThreshold();
  updateModelHelp();
  updateEvaluationControls();
  $("config-dirty").hidden = true;
}

function clearReport() {
  stopWatching();
  state.selection += 1;
  state.report = null;
  state.run = null;
  state.charts = [];
  $("report-content").hidden = true;
  $("empty-state").hidden = false;
  setBusy(false);
  showError("");
  $("config-dirty").hidden = true;
  renderHistory();
  setView(state.view);
}

function renderHistory() {
  const container = $("history-list");
  container.innerHTML = "";
  if (!state.runs.length) { container.innerHTML = '<p class="muted small">还没有实验记录。<br>运行第一个研究实验吧。</p>'; return; }
  for (const run of state.runs.slice(0, 12)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `history-item${state.run?.id === run.id ? " selected" : ""}`;
    const status = { queued: "排队中", running: "训练中", completed: "已完成", failed: "失败" }[run.status] || run.status;
    const date = run.created_at ? new Date(run.created_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
    button.innerHTML = `<strong>${escapeHTML(run.dataset_name || "研究实验")}</strong><small><span>${escapeHTML(modelLabel(run.config?.model))} · ${escapeHTML(date)}</span><span class="status-dot ${run.status === "failed" ? "failed" : ""}">${escapeHTML(status)}</span></small>`;
    button.title = `${run.dataset_name || "研究实验"} · ${status} · ${run.id}`;
    button.addEventListener("click", () => {
      if (state.busy && research.pairIds) { toast("双模型正在运行，完成后会自动打开对比；请稍后切换实验。"); return; }
      openRun(run.id);
    });
    container.appendChild(button);
  }
}

async function refreshHistory() {
  const result = await api("/api/runs");
  state.runs = Array.isArray(result) ? result : result.runs || [];
  renderHistory();
  renderCompareOptions();
}

async function openRun(id, silent = false) {
  stopWatching();
  const token = ++state.selection;
  showError("");
  try {
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (token !== state.selection) return;
    state.run = run;
    if (state.datasets.some((item) => item.id === run.dataset_id)) $("dataset-select").value = run.dataset_id;
    updateDataset();
    applyConfig(run.config);
    renderHistory();
    if (run.status === "completed" && run.report) {
      setBusy(false);
      renderReport(run);
      if (!silent) toast("已载入实验报告，右侧显示该实验的原始参数。");
    } else if (run.status === "failed") {
      state.report = null;
      $("report-content").hidden = true;
      $("empty-state").hidden = false;
      setBusy(false);
      showError(`这次实验未完成：${run.error || "请调整数据或参数后重试。"}`);
    } else {
      state.report = null;
      $("report-content").hidden = true;
      $("empty-state").hidden = true;
      watchRun(run, token);
    }
  } catch (error) { if (token === state.selection) { setBusy(false); showError(error.message); } }
}

function watchRun(run, token) {
  setBusy(true);
  const started = Date.now();
  $("run-elapsed").textContent = "0s";
  state.clockTimer = setInterval(() => { $("run-elapsed").textContent = `${Math.floor((Date.now() - started) / 1000)}s`; }, 1000);
  let failures = 0;
  const poll = async () => {
    if (token !== state.selection) return;
    try {
      const current = await api(`/api/runs/${encodeURIComponent(run.id)}`);
      if (token !== state.selection) return;
      failures = 0;
      state.run = current;
      $("run-status-title").textContent = current.status === "queued" ? "任务已提交，等待本地计算资源" : "模型正在学习历史行情";
      $("run-status-detail").textContent = current.status === "queued" ? "已有实验完成后会自动开始，请保持本地服务运行。" : "构建时序特征 → 训练分类与回归模型 → 样本外预测与成本回测";
      if (current.status === "completed") {
        stopWatching(); setBusy(false); renderReport(current);
        refreshHistory().then(() => finishPairIfReady(current.id)).catch(() => {});
        toast("训练完成。现在可以查看回测结果、逐日预测并导出报告。");
        return;
      }
      if (current.status === "failed") {
        stopWatching(); setBusy(false); $("empty-state").hidden = false;
        showError(`实验未完成：${current.error || "请检查数据后重试。"}`);
        refreshHistory().catch(() => {}); return;
      }
    } catch (error) {
      if (token !== state.selection) return;
      failures += 1;
      if (failures >= 5) {
        stopWatching(); setBusy(false); $("empty-state").hidden = false;
        showError(`${error.message}\n页面暂时无法读取任务状态，服务端任务可能仍在执行；稍后从左侧实验记录重新打开。`);
        return;
      }
      $("run-status-detail").textContent = "连接暂时中断，正在重新读取任务状态…";
    }
    if (token === state.selection) state.pollTimer = setTimeout(poll, failures ? 2200 : 850);
  };
  poll();
}

async function submitRun(event) {
  event.preventDefault();
  if (state.busy || !$("run-form").reportValidity()) return;
  const config = formConfig();
  showError("");
  setBusy(true);
  try {
    const run = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    await refreshHistory().catch(() => {});
    await openRun(run.id, true);
  } catch (error) { setBusy(false); showError(error.message); }
}

function writeMetric(id, value, useSign = false, color = true, digits = 2) {
  $(id).textContent = percent(value, digits, useSign);
  $(id).classList.remove("positive", "negative");
  if (color && signedClass(value)) $(id).classList.add(signedClass(value));
}

function renderReport(run) {
  const report = run.report;
  if (!report) { showError("这次实验没有可读取的报告。"); return; }
  state.report = report; state.run = run; state.page = 0;
  $("empty-state").hidden = true;
  $("report-content").hidden = false;
  const metric = report.metrics || {}, latest = report.latest || {}, config = report.config || run.config || {};
  writeMetric("metric-return", metric.total_return, true);
  writeMetric("metric-benchmark", metric.benchmark_return, true);
  writeMetric("metric-drawdown", metric.max_drawdown);
  writeMetric("metric-accuracy", metric.accuracy, false, false, 1);
  $("metric-auc").textContent = `样本外 AUC ${number(metric.auc, 3)}`;
  $("metric-return-detail").textContent = `${number(metric.test_sessions, 0)} 日样本外 · 已扣交易成本`;
  $("initial-capital").textContent = `¥${number(config.initial_capital, 0)}`;
  $("trade-count").textContent = `${number(metric.trades, 0)} 次`;
  $("win-rate").textContent = metric.trades ? percent(metric.win_rate, 1) : "无交易";
  $("annualized-return").textContent = percent(metric.annualized_return, 2, true);
  $("prediction-date").textContent = `截至 ${shortDate(latest.as_of)} 收盘`;
  $("probability-up").textContent = percent(latest.probability_up, 1);
  const angle = Math.max(0, Math.min(1, Number(latest.probability_up) || 0)) * 360;
  $("probability-ring").style.background = `conic-gradient(var(--teal) 0deg,var(--teal) ${angle}deg,var(--border) ${angle}deg)`;
  const active = latest.signal === "BUY";
  $("latest-signal").textContent = active ? "模型信号 · 入场" : "模型信号 · 等待";
  $("latest-signal").classList.toggle("wait", !active);
  writeMetric("predicted-return", latest.predicted_return, true);
  $("reference-close").textContent = `参考收盘价 ${number(latest.reference_close)}`;
  $("prediction-context").textContent = `上涨概率${active ? "达到" : "未达到"} ${percent(latest.threshold ?? config.threshold, 0)} 入场阈值。模型只使用截至 ${shortDate(latest.model_train_end)} 的训练集拟合。`;
  $("feature-subtitle").textContent = config.model === "logistic" ? "标准化系数绝对值 · 归一化占比" : "树模型特征重要性 · 归一化占比";
  const features = [...(report.features || [])].sort((a, b) => b.importance - a.importance).slice(0, 6);
  const maximum = Math.max(...features.map((feature) => Number(feature.importance) || 0), .001);
  $("feature-list").innerHTML = features.map((feature) => `<div class="feature-row" title="${escapeHTML(feature.name)}；最新特征值 ${escapeHTML(number(feature.value, 4))}"><div class="feature-meta"><span>${escapeHTML(feature.label || feature.name)}</span><span>${escapeHTML(percent(feature.importance, 1))}</span></div><div class="feature-track"><div class="feature-bar" style="width:${Math.max(0, (Number(feature.importance) || 0) / maximum * 100)}%"></div></div></div>`).join("");
  for (const key of ["train", "validation", "test"]) {
    const split = report.split?.[key] || {};
    $(`${key}-dates`).textContent = `${shortDate(split.start)} → ${shortDate(split.end)}`;
    $(`${key}-rows`).textContent = `${number(split.rows, 0)} 个信号样本`;
    $(`split-${key}-bar`).style.flex = Math.max(1, split.rows || 1);
  }
  $("split-description").textContent = config.evaluation === "walk_forward" ? "这里展示最后一折；完整时间边界与逐折结果见稳健性分析。" : "按时间约 60% / 20% / 20% 切分，两个边界各隔离 1 行。";
  $("gap-label").textContent = `GAP ${report.split?.gap ?? 1}`;
  const detailItems = [["分类 / 回归模型", report.model?.name || modelLabel(config.model)], ["历史特征数", number(report.model?.feature_count ?? report.features?.length, 0)],
    ["最新有效阈值", percent(latest.threshold ?? config.threshold, 0)], ["随机种子", String(config.seed ?? "—")], ["验证集准确率", percent(report.validation_metrics?.accuracy, 1)],
    ["验证集 AUC", number(report.validation_metrics?.auc, 3)], ["测试集精确率", percent(metric.precision, 1)], ["测试集召回率", percent(metric.recall, 1)],
    ["收益预测 MAE", percent(metric.return_mae, 3)], ["策略年化夏普", number(metric.sharpe, 2)], ["单边手续费 / 滑点", `${number(config.cost_bps, 1)} / ${number(config.slippage_bps, 1)} bps`], ["交易日占比", percent(metric.exposure, 1)]];
  $("model-details-grid").innerHTML = detailItems.map(([label, value]) => `<div class="detail-item"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong></div>`).join("");
  $("report-notes").innerHTML = (report.notes || []).map((note) => `<li>${escapeHTML(note)}</li>`).join("");
  $("audit-description").textContent = `${number(report.predictions?.length || 0, 0)} 条样本外预测 · 按交易日倒序`;
  $("report-id").textContent = `${run.id.startsWith("bundled-") ? "随项目附带的真实计算报告" : "实验 " + run.id.slice(0, 12)} · ${modelLabel(config.model)} · Seed ${config.seed ?? "—"}`;
  setDownload("export-csv", `/api/runs/${encodeURIComponent(run.id)}/export?format=csv`);
  setDownload("export-json", `/api/runs/${encodeURIComponent(run.id)}/export?format=json`);
  setDownload("export-html", `/api/runs/${encodeURIComponent(run.id)}/export?format=html`);
  $("export-model").hidden = !run.has_model;
  if (run.has_model) setDownload("export-model", `/api/runs/${encodeURIComponent(run.id)}/model`);
  resetPredictionFilters();
  renderPredictions();
  renderMonthlyReturns();
  renderRobustness();
  research.comparisonCache.set(run.id, run);
  $("config-dirty").hidden = true;
  setView(state.view);
  renderHistory();
}

function renderPredictions() {
  const predictions = filteredPredictions();
  const total = state.report?.predictions?.length || 0;
  $("filter-summary").textContent = $("filter-start").value && $("filter-end").value && $("filter-start").value > $("filter-end").value ? "起始日期晚于结束日期，请调整日期范围。" : `筛选 ${predictions.length} / ${total} 条记录 · 本筛选仅影响表格，不改变回测指标` ;
  $("export-filtered").disabled = !predictions.length;
  const pages = Math.max(1, Math.ceil(predictions.length / state.pageSize));
  state.page = Math.min(Math.max(0, state.page), pages - 1);
  const visible = predictions.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize);
  $("predictions-body").innerHTML = visible.map((row) => `<tr><td>${escapeHTML(row.signal_date)}</td><td>${escapeHTML(row.date)}</td><td>${escapeHTML(percent(row.probability_up, 1))}</td><td class="${signedClass(row.predicted_return)}">${escapeHTML(percent(row.predicted_return, 2, true))}</td><td class="${signedClass(row.actual_return)}">${escapeHTML(percent(row.actual_return, 2, true))}</td><td><span class="table-signal ${row.signal === "BUY" ? "" : "wait"}">${row.signal === "BUY" ? "入场" : "空仓"}</span></td><td class="${row.correct ? "table-correct" : "table-incorrect"}">${row.correct ? "✓ 正确" : "× 偏差"}</td></tr>`).join("") || '<tr><td colspan="7">当前条件下没有预测记录，请调整筛选条件。</td></tr>';
  $("table-page").textContent = `${state.page + 1} / ${pages}`;
  $("table-prev").disabled = state.page <= 0;
  $("table-next").disabled = state.page >= pages - 1;
}

function setView(view) {
  state.view = view;
  const toolsMode = view === "workbench";
  $("tool-workspace").hidden = !toolsMode;
  document.querySelector(".config-column").hidden = toolsMode;
  document.querySelector(".dashboard-layout").classList.toggle("tool-mode", toolsMode);
  if (toolsMode) loadTools();
  $("comparison-workspace").hidden = view !== "compare";
  $("report-content").hidden = view === "compare" || toolsMode || !state.report;
  $("empty-state").hidden = view === "compare" || toolsMode || Boolean(state.report) || state.busy;
  if (view === "compare") { renderCompareOptions(); loadComparison(); }
  const titles = { overview: ["研究概览", "让每一次预测，都有据可循", "用机器学习观察趋势，用样本外回测检验想法。"], backtest: ["策略回测", "好的假设，经得起历史检验", "沿着时间回看每一次信号，理解收益、成本与风险。"], model: ["模型解释", "打开模型，看见预测的依据", "查看特征、数据切分与评估指标，建立可解释的研究过程。"] };
  titles.compare = ["实验对比", "换一个模型，让结果回答", "在相同数据上比较两次实验，理解准确率、成本与风险。"] ;
  titles.robustness = ["稳健性分析", "向前一步，再检验一次", "沿时间评估模型，观察阈值、交易成本与输入质量。"] ;
  titles.workbench = ["研究工具", "从趋势，到可检验的观察", "技术分析、自选预警、策略扫描与模拟持仓，一起留下研究依据。"] ;
  const title = titles[view] || titles.overview;
  $("view-name").textContent = title[0];
  $("page-title").innerHTML = `${escapeHTML(title[1])}<span>.</span>`;
  $("page-description").textContent = title[2];
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  for (const section of $("report-content").querySelectorAll(".section-overview,.section-backtest,.section-model,.section-robustness")) section.hidden = !section.classList.contains(`section-${view}`);
  requestAnimationFrame(drawCharts);
}

function compactNumber(value) {
  if (Math.abs(value) >= 1e8) return `${number(value / 1e8, 2)}亿`;
  if (Math.abs(value) >= 1e4) return `${number(value / 1e4, 2)}万`;
  if (Math.abs(value) >= 1000) return number(value, 0);
  return number(value, 2);
}

function drawCharts() {
  if (state.view === "workbench") { drawToolCharts(); return; }
  if (state.view === "compare") { drawComparisonChart(); return; }
  if (!state.report || $("report-content").hidden) return;
  drawPriceChart();
  const equity = [...(state.report.equity || [])];
  const config = state.report.config || state.run?.config || {};
  if (equity.length && Number.isFinite(config.initial_capital)) equity.unshift({ date: state.report.predictions?.[0]?.signal_date || "起点", strategy: config.initial_capital, benchmark: config.initial_capital });
  if (!$("equity-chart").closest("section").hidden) lineChart("equity-chart", equity, [{ key: "strategy", label: "模型策略", color: chartColor("--teal"), fill: true }, { key: "benchmark", label: "日内基准", color: chartColor("--blue"), dash: true }]);
  if (!$("drawdown-chart").closest("section").hidden) lineChart("drawdown-chart", state.report.equity || [], [{ key: "drawdown", label: "回撤", color: chartColor("--red"), fill: true }], { percent: true, zeroTop: true });
}

function lineChart(id, data, series, options = {}) {
  const container = $(id);
  if (!data.length) { container.innerHTML = '<div class="chart-empty">暂无曲线数据</div>'; return; }
  const width = Math.max(280, container.clientWidth), height = Math.max(120, container.clientHeight);
  const padding = { top: 12, right: 15, bottom: 29, left: width < 450 ? 49 : 57 };
  const plotWidth = width - padding.left - padding.right, plotHeight = height - padding.top - padding.bottom;
  const values = data.flatMap((row) => series.map((item) => row[item.key])).filter(Number.isFinite);
  if (!values.length) { container.innerHTML = '<div class="chart-empty">没有有效数值</div>'; return; }
  let low = Math.min(...values), high = Math.max(...values);
  const buffer = (high - low || Math.abs(high) * .01 || 1) * .14;
  low -= buffer; high += buffer;
  if (options.zeroTop) { high = 0; low = Math.min(low, -0.001); }
  const formatValue = (value) => options.percent ? percent(value, 2) : compactNumber(value);
  const x = (index) => padding.left + index / Math.max(1, data.length - 1) * plotWidth;
  const y = (value) => padding.top + (high - value) / (high - low) * plotHeight;
  let markup = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${id === "price-chart" ? "历史收盘价" : id === "drawdown-chart" ? "回撤" : id === "compare-chart" ? "实验归一化净值" : "策略与基准资金"}曲线，${escapeHTML(data[0].date)}至${escapeHTML(data[data.length - 1].date)}"><defs>`;
  for (const item of series) markup += `<linearGradient id="${id}-${item.key}-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${item.color}" stop-opacity=".14"/><stop offset="100%" stop-color="${item.color}" stop-opacity="0"/></linearGradient>`;
  markup += "</defs>";
  for (let tick = 0; tick <= 3; tick += 1) {
    const value = low + (high - low) * tick / 3, position = y(value);
    markup += `<line x1="${padding.left}" x2="${width - padding.right}" y1="${position}" y2="${position}" stroke="${chartColor("--border")}" stroke-width="1" stroke-dasharray="3 5"/><text x="${padding.left - 9}" y="${position + 3}" text-anchor="end" fill="${chartColor("--muted")}" font-family="Segoe UI, sans-serif" font-size="9">${escapeHTML(formatValue(value))}</text>`;
  }
  const labels = width < 450 ? 3 : 5;
  for (let tick = 0; tick < labels; tick += 1) {
    const index = Math.round(tick / (labels - 1) * (data.length - 1));
    const date = String(data[index].date || "");
    const label = date.length >= 10 ? `${date.slice(2, 4)}/${date.slice(5, 7)}/${date.slice(8, 10)}` : date;
    markup += `<text x="${x(index)}" y="${height - 9}" text-anchor="${tick === 0 ? "start" : tick === labels - 1 ? "end" : "middle"}" fill="${chartColor("--muted")}" font-family="Segoe UI, sans-serif" font-size="9">${escapeHTML(label)}</text>`;
  }
  for (const item of [...series].reverse()) {
    const points = data.map((row, index) => [x(index), Number.isFinite(row[item.key]) ? y(row[item.key]) : NaN]).filter((point) => point.every(Number.isFinite));
    const path = points.map((point, index) => `${index ? "L" : "M"}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(" ");
    const fillBaseline = options.zeroTop ? padding.top : height - padding.bottom;
    if (item.fill && points.length) markup += `<path d="${path} L${points[points.length - 1][0]},${fillBaseline} L${points[0][0]},${fillBaseline} Z" fill="url(#${id}-${item.key}-fill)"/>`;
    markup += `<path d="${path}" stroke="${item.color}" fill="none" stroke-width="${item.fill ? "1.8" : "1.5"}" stroke-linejoin="round" stroke-linecap="round"${item.dash ? ' stroke-dasharray="4 3" opacity=".8"' : ""}/>`;
  }
  markup += `<g class="chart-crosshair" visibility="hidden"><line y1="${padding.top}" y2="${height - padding.bottom}" stroke="#77998d" stroke-width="1" stroke-dasharray="3 3"/>${series.map((item) => `<circle r="3.2" fill="${item.color}" stroke="#132a29" stroke-width="2"/>`).join("")}</g><rect class="chart-hitarea" x="${padding.left}" y="${padding.top}" width="${plotWidth}" height="${plotHeight}" fill="transparent" style="cursor:crosshair"/></svg><div class="chart-tooltip" hidden></div>`;
  container.innerHTML = markup;
  const svg = container.querySelector("svg"), crosshair = container.querySelector(".chart-crosshair"), tooltip = container.querySelector(".chart-tooltip");
  const hit = container.querySelector(".chart-hitarea");
  hit.addEventListener("pointermove", (event) => {
    const box = svg.getBoundingClientRect();
    const relativeX = (event.clientX - box.left) * width / box.width;
    const index = Math.max(0, Math.min(data.length - 1, Math.round((relativeX - padding.left) / plotWidth * (data.length - 1))));
    const row = data[index], position = x(index);
    crosshair.setAttribute("visibility", "visible");
    const line = crosshair.querySelector("line"); line.setAttribute("x1", position); line.setAttribute("x2", position);
    crosshair.querySelectorAll("circle").forEach((circle, seriesIndex) => {
      const value = row[series[seriesIndex].key];
      circle.setAttribute("visibility", Number.isFinite(value) ? "visible" : "hidden");
      if (Number.isFinite(value)) { circle.setAttribute("cx", position); circle.setAttribute("cy", y(value)); }
    });
    tooltip.innerHTML = `<strong>${escapeHTML(row.date)}</strong>${series.map((item) => `<span><i style="background:${item.color}"></i>${escapeHTML(item.label)} ${escapeHTML(options.percent ? percent(row[item.key], 2) : number(row[item.key], 2))}</span>`).join("")}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(4, Math.min(container.clientWidth - tooltip.offsetWidth - 4, position + 12))}px`;
    tooltip.style.top = "3px";
  });
  hit.addEventListener("pointerleave", () => { crosshair.setAttribute("visibility", "hidden"); tooltip.hidden = true; });
}

async function upload(event) {
  event.preventDefault();
  const file = $("csv-file").files[0];
  if (!file) return;
  $("upload-error").hidden = true;
  if (file.size > (state.limits.max_upload_mb || 8) * 1024 * 1024) {
    $("upload-error").textContent = `文件不能超过 ${state.limits.max_upload_mb || 8} MB。`;
    $("upload-error").hidden = false; return;
  }
  const form = new FormData(); form.append("file", file); form.append("name", $("dataset-upload-name").value.trim());
  $("upload-submit").disabled = true;
  $("upload-submit").textContent = "正在校验与导入…";
  try {
    const dataset = await api("/api/datasets/upload", { method: "POST", body: form });
    if (!state.datasets.some((item) => item.id === dataset.id)) state.datasets.push(dataset);
    clearReport(); renderDatasets(dataset.id);
    $("upload-dialog").close(); $("upload-form").reset(); $("upload-file-label").textContent = "点击选择 CSV 文件";
    toast(`已导入 ${number(dataset.rows, 0)} 条行情，可开始训练。`);
  } catch (error) { $("upload-error").textContent = error.message; $("upload-error").hidden = false; }
  finally { $("upload-submit").disabled = false; $("upload-submit").innerHTML = "导入数据集 <span>↗</span>"; }
}

async function openDocs() {
  $("docs-dialog").showModal();
  $("docs-list").innerHTML = '<p class="muted">正在读取项目文档…</p>';
  try {
    const result = await api("/api/documents");
    const documents = result.documents || [];
    $("docs-list").innerHTML = "";
    documents.forEach((doc, index) => {
      const link = document.createElement("a");
      link.className = "doc-link";
      // Only construct a same-origin link from the document name; never trust arbitrary URLs.
      link.href = `/api/documents/${encodeURIComponent(doc.name)}`;
      link.target = "_blank"; link.rel = "noopener noreferrer";
      link.innerHTML = `<span class="doc-number">${String(index + 1).padStart(2, "0")}</span><div><strong>${escapeHTML(doc.title || doc.name)}</strong><small>${escapeHTML(doc.name)} · 新窗口打开</small></div><span>↗</span>`;
      $("docs-list").appendChild(link);
    });
    const apiLink = document.createElement("a");
    apiLink.className = "doc-link"; apiLink.href = "/docs"; apiLink.target = "_blank"; apiLink.rel = "noopener noreferrer";
    apiLink.innerHTML = '<span class="doc-number">API</span><div><strong>交互式接口文档</strong><small>FastAPI / Swagger · 需浏览器可加载文档资源</small></div><span>↗</span>';
    $("docs-list").appendChild(apiLink);
  } catch (error) { $("docs-list").textContent = error.message; }
}

async function initialize() {
  $("run-button").disabled = true;
  try {
    const bootstrap = await api("/api/bootstrap");
    state.datasets = bootstrap.datasets || [];
    state.runs = bootstrap.runs || [];
    state.limits = bootstrap.limits || {};
    $("min-rows").textContent = state.limits.min_rows || 300;
    $("max-upload").textContent = state.limits.max_upload_mb || 8;
    renderDatasets(); applyConfig(bootstrap.defaults); renderHistory(); renderCompareOptions();
    const latest = state.runs.find((run) => run.status === "completed") || state.runs.find((run) => ["queued", "running"].includes(run.status));
    if (latest) await openRun(latest.id, true);
    const requestedView = new URLSearchParams(location.search).get("view");
    if (["compare", "workbench"].includes(requestedView)) setView(requestedView);
  } catch (error) {
    $("dataset-select").innerHTML = '<option value="">未能连接本地服务</option>';
    showError(error.message);
  }
}

$("run-form").addEventListener("submit", submitRun);
$("threshold").addEventListener("input", updateThreshold);
$("model-select").addEventListener("change", updateModelHelp);
$("dataset-select").addEventListener("change", () => { clearReport(); updateDataset(); });
$("refresh-history").addEventListener("click", () => refreshHistory().then(() => toast("实验记录已更新。")).catch((error) => showError(error.message)));
$("upload-button").addEventListener("click", () => { $("upload-error").hidden = true; $("upload-dialog").showModal(); });
$("upload-form").addEventListener("submit", upload);
$("csv-file").addEventListener("change", () => { $("upload-file-label").textContent = $("csv-file").files[0]?.name || "点击选择 CSV 文件"; });
$("docs-button").addEventListener("click", openDocs);
document.querySelectorAll(".dialog-close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) { const bounds = dialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close(); } }));
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
$("table-prev").addEventListener("click", () => { state.page -= 1; renderPredictions(); });
$("table-next").addEventListener("click", () => { state.page += 1; renderPredictions(); });
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawCharts, 120); });
window.addEventListener("pagehide", stopWatching);
updateThreshold();
bindResearchControls();
bindRobustnessControls();
bindToolControls();
initialize();
