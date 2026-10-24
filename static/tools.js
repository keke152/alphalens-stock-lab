"use strict";
const toolkit = { active: "technical", analysis: null, workspace: null, account: null, candles: 90, sequence: 0, paperSequence: 0 };
const ruleNames = {cost:"成本盈亏",change:"收盘涨跌",volume:"成交量",ma_cross:"均线交叉",rsi:"RSI",gap:"跳空",trailing:"区间回撤"};
const percentWatchFields = new Set(["profit","loss","change","gap","trailing_activation","trailing_drawdown"]);
const riskFields = [
  ["max_position","单标的仓位上限 %",true,.1,100], ["max_exposure","总仓位上限 %",true,.1,100],
  ["max_holdings","最大持仓标的数",false,1,20], ["max_drawdown","资产回撤触发线 %",true,.1,90],
  ["daily_loss","当日亏损触发线 %",true,.1,90], ["consecutive_loss_days","减半仓位：连续亏损日数",false,2,10],
  ["cooldown_sessions","回撤触发暂停记录日数",false,1,30], ["stop_loss","单标的参考止损 %",true,.1,90],
  ["take_profit","单标的参考止盈 %",true,.1,300], ["trailing_activation","移动止盈激活幅度 %",true,.1,300],
  ["trailing_drawdown","移动止盈峰值回撤 %",true,.1,90], ["fee_bps","单边费用 bps",false,0,100],
  ["slippage_bps","单边滑点 bps",false,0,100]
];

function toolQuery() { return $("tool-asof").value ? `?as_of=${encodeURIComponent($("tool-asof").value)}` : ""; }
function toolStatus(message) { $("tools-status").textContent = message; }

function syncToolDatasets() {
  if (!$("tool-dataset")) return;
  const selected = $("dataset-select").value;
  for (const id of ["tool-dataset","scan-benchmark"]) {
    const select = $(id), previous = select.value;
    select.innerHTML = id === "scan-benchmark" ? '<option value="">未提供 · 对应条件显示未知</option>' : "";
    for (const dataset of state.datasets) {
      const option = document.createElement("option"); option.value=dataset.id;option.textContent=dataset.name;select.appendChild(option);
    }
    select.value = id === "tool-dataset" ? selected : previous;
  }
  const dataset=state.datasets.find(item=>item.id===selected);
  if(dataset) {
    $("tool-asof").min=dataset.start;$("tool-asof").max=dataset.end;
    $("paper-trade-date").min=dataset.start;$("paper-trade-date").max=dataset.end;
  }
}

async function loadTools() {
  if (state.view !== "workbench" || !$("tool-dataset").value) return;
  const sequence=++toolkit.sequence;
  toolStatus("正在读取本地日线和研究工具状态…");
  try {
    const [snapshot, workspace] = await Promise.all([
      api(`/api/datasets/${encodeURIComponent($("tool-dataset").value)}/analysis${toolQuery()}`),
      api(`/api/research-tools${toolQuery()}`)
    ]);
    if(sequence!==toolkit.sequence || state.view!=="workbench")return;
    toolkit.analysis=snapshot;toolkit.workspace=workspace;
    renderTechnical();renderWatchlist();
    if(!$("paper-trade-date").value)$("paper-trade-date").value=snapshot.as_of;
    toolStatus(`行情截至 ${snapshot.as_of} · 可用 ${snapshot.rows_available} 条记录 · ${snapshot.dataset.source==="synthetic"?"合成演示数据":"本地导入快照"} · 综合分未启用`);
    if(toolkit.active==="portfolio")await loadPaper();
    requestAnimationFrame(drawToolCharts);
  } catch(error) { if(sequence===toolkit.sequence)toolStatus(error.message); }
}

function setTool(active) {
  toolkit.active=active;
  document.querySelectorAll("[data-tool]").forEach(button=>{
    button.classList.toggle("active",button.dataset.tool===active);
    button.setAttribute("aria-pressed",String(button.dataset.tool===active));
  });
  document.querySelectorAll("[data-tool-panel]").forEach(panel=>{panel.hidden=panel.dataset.toolPanel!==active;});
  if(active==="portfolio")loadPaper().catch(error=>toolStatus(error.message));
  requestAnimationFrame(drawToolCharts);
}

function renderTechnical() {
  const a=toolkit.analysis;if(!a)return;
  const row=a.latest;
  $("technical-score").textContent=a.score.value===null?"—":`${a.score.value} / 100`;
  $("technical-score-label").textContent=`${a.score.label} · 技术规则分，非上涨概率`;
  $("tool-rsi").textContent=number(row.rsi14,1);$("tool-macd").textContent=number(row.macd,3);
  $("tool-dif-dea").textContent=`DIF ${number(row.dif,3)} / DEA ${number(row.dea,3)}`;
  $("tool-atr").textContent=number(row.atr14,3);
  $("technical-date").textContent=`截至 ${a.as_of} · OHLC K线 · MA5 / MA20 / MA60`;
  const reviewUrl=`/api/research-tools/export?dataset_id=${encodeURIComponent(a.dataset.id)}&as_of=${encodeURIComponent(a.as_of)}`;
  setDownload("export-daily-review",reviewUrl+"&format=html");setDownload("export-review-json",reviewUrl+"&format=json");
  $("score-coverage").textContent=`数据覆盖 ${percent(a.score.coverage,0)}`;
  $("score-components").innerHTML=a.score.components.map(c=>`<div class="score-component"><strong>${escapeHTML(c.label)} · ${c.weight} 分</strong><span>${c.passed===null?"预热不足":c.passed?"✓ 规则满足":"○ 规则未满足"}</span><span>${Object.entries(c.values).map(([k,v])=>`${escapeHTML(k)} ${number(v,3)}`).join("<br>")}</span></div>`).join("");
  $("score-dimensions").innerHTML=a.score.dimensions.map(d=>`<div class="${d.status==="available"?"available":""}">${escapeHTML(d.name)} · ${percent(d.weight,0)}<small>${d.status==="available"?"已计算技术规则分":"缺少数据 · 不计分"}</small></div>`).join("");
  $("paper-fill-note").textContent=`当前数据：${a.dataset.name} · 截止日最新收盘 ${number(row.close,4)}（${a.as_of}）。成交按你选择的实际记录日重新取价。`;
}

function drawCandlesticks() {
  const container=$("candlestick-chart"), a=toolkit.analysis;
  if(!a || toolkit.active!=="technical" || state.view!=="workbench")return;
  const data=a.series.slice(-toolkit.candles), width=Math.max(container.clientWidth-32,320),height=340;
  const padding={left:58,right:18,top:20,bottom:35}, w=width-padding.left-padding.right,h=height-padding.top-padding.bottom;
  const values=data.flatMap(row=>[row.low,row.high,row.ma5,row.ma20,row.ma60]).filter(Number.isFinite);
  const low=Math.min(...values),high=Math.max(...values),span=Math.max(high-low,.001);
  const y=value=>padding.top+(high-value)/span*h,x=i=>padding.left+(i+.5)*w/data.length;
  const bodyWidth=Math.max(1.5,w/data.length*.58);
  let content="";
  for(let i=0;i<5;i++) { const value=low+span*i/4,py=y(value);content+=`<line x1="${padding.left}" x2="${width-padding.right}" y1="${py}" y2="${py}" stroke="${chartColor("--border")}" stroke-dasharray="3 5"/><text x="${padding.left-7}" y="${py+4}" text-anchor="end">${number(value,2)}</text>`; }
  data.forEach((row,i)=>{
    const cx=x(i),style=row.close>row.open?"candle-up":"candle-down",top=y(Math.max(row.open,row.close)),bottom=y(Math.min(row.open,row.close));
    content+=`<g class="${style}"><title>${escapeHTML(row.date)} 开 ${row.open} 高 ${row.high} 低 ${row.low} 收 ${row.close} 量 ${row.volume}</title><line x1="${cx}" x2="${cx}" y1="${y(row.high)}" y2="${y(row.low)}"/><rect x="${cx-bodyWidth/2}" y="${top}" width="${bodyWidth}" height="${Math.max(bottom-top,1)}"/></g>`;
  });
  for(const [key,color] of [["ma5",chartColor("--amber")],["ma20",chartColor("--violet")],["ma60",chartColor("--blue")]]) {
    const points=data.map((row,i)=>Number.isFinite(row[key])?`${x(i)},${y(row[key])}`:null).filter(Boolean).join(" ");
    content+=`<polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.4"/>`;
  }
  for(const i of [0,Math.floor(data.length/2),data.length-1])content+=`<text x="${x(i)}" y="${height-10}" text-anchor="middle">${escapeHTML(data[i].date.slice(2))}</text>`;
  container.innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="日线 K线及三条均线">${content}</svg>`;
}

function drawToolCharts() {
  if(state.view!=="workbench")return;
  if(toolkit.active==="technical" && toolkit.analysis) {
    drawCandlesticks();
    const mode=$("indicator-select").value,data=toolkit.analysis.series.slice(-toolkit.candles);
    const definitions={macd:[["dif","DIF","--teal"],["dea","DEA","--amber"],["macd","2×差值","--blue"]],rsi:[["rsi14","RSI14","--teal"]],kdj:[["k","K","--teal"],["d","D","--blue"],["j","J","--amber"]],boll:[["close","收盘","--teal"],["boll_upper","上轨","--blue"],["boll_mid","中轨","--amber"],["boll_lower","下轨","--violet"]]};
    lineChart("indicator-chart",data,definitions[mode].map(([key,label,color])=>({key,label,color:chartColor(color)})));
  }
  if(toolkit.active==="portfolio" && toolkit.account?.history.length)lineChart("paper-equity-chart",toolkit.account.history,[{key:"equity",label:"虚拟资产",color:chartColor("--teal"),fill:true}]);
  else if(toolkit.active==="portfolio")$("paper-equity-chart").innerHTML='<p class="chart-empty">记录模拟成交后显示资产轨迹。</p>';
}

function renderWatchlist() {
  const workspace=toolkit.workspace;if(!workspace)return;
  $("watchlist-body").innerHTML=workspace.watchlist.map(item=>`<tr><td>${escapeHTML(item.dataset_name)}</td><td>${escapeHTML(item.snapshot.as_of||"—")}</td><td>${number(item.snapshot.close,3)}</td><td>${percent(item.snapshot.cost_return,2,true)}</td><td>${item.rules.enabled.length} / 7</td><td><button class="table-action" data-edit-watch="${escapeHTML(item.dataset_id)}">设置</button><button class="table-action danger" data-remove-watch="${escapeHTML(item.dataset_id)}">移出</button></td></tr>`).join("")||'<tr><td colspan="6">自选清单为空。选择一个数据集，再点击“加入自选”。</td></tr>';
  const monitor=workspace.monitor;
  $("monitor-enabled").checked=monitor.enabled;$("monitor-interval").value=String(monitor.interval_seconds);
  $("monitor-status").textContent=`${monitor.enabled?"后台检查已启用":"后台检查未启用"} · 最近检查 ${monitor.last_scan?new Date(monitor.last_scan).toLocaleString("zh-CN"):"尚无"}${monitor.last_error?" · "+monitor.last_error:""} · 来源为本地快照`;
  renderAlerts();
}

function renderAlerts() {
  const all=toolkit.workspace?.alerts||[],unread=all.filter(row=>!row.acknowledged).length;
  const rows=all.filter(row=>!$("alerts-unread").checked||!row.acknowledged);
  $("alerts-count").textContent=`最近 ${all.length} 条 · ${unread} 条未读`;
  $("alerts-list").innerHTML=rows.map(row=>`<article class="alert-item ${row.acknowledged?"read":""}"><div><h4>${escapeHTML(row.title)} · ${escapeHTML(row.dataset_name)}</h4><p>${escapeHTML(row.detail)}</p><small>行情日 ${escapeHTML(row.as_of)} · 本地快照 · ${escapeHTML(new Date(row.created_at).toLocaleString("zh-CN"))}</small></div>${row.acknowledged?'<span class="tiny-pill">已读</span>':`<button class="secondary-button" data-read-alert="${escapeHTML(row.id)}">标为已读</button>`}</article>`).join("")||'<p class="tool-caption">当前没有符合条件的预警。系统只在规则实际触发时生成记录。</p>';
}

function openWatchDialog(id) {
  const dataset=state.datasets.find(row=>row.id===id),item=toolkit.workspace?.watchlist.find(row=>row.dataset_id===id);
  if(!dataset||!toolkit.workspace)return;
  const rules=item?.rules||toolkit.workspace.rule_defaults;
  $("watch-dataset-id").value=id;$("watch-dataset-name").textContent=dataset.name;
  $("watch-cost").value=item?.cost_price??"";$("watch-cost-date").value=item?.cost_date||toolkit.analysis?.as_of||dataset.end;
  $("watch-cost-date").min=dataset.start;$("watch-cost-date").max=dataset.end;
  document.querySelectorAll("[data-watch-field]").forEach(input=>{input.value=numberForInput(rules[input.dataset.watchField]*(percentWatchFields.has(input.dataset.watchField)?100:1));});
  document.querySelectorAll("[data-watch-rule]").forEach(input=>{input.checked=rules.enabled.includes(input.dataset.watchRule);});
  $("watch-form-error").hidden=true;$("watch-dialog").showModal();
}

function numberForInput(value) {return String(Math.round(value*1000000)/1000000);}

async function saveWatch(event) {
  event.preventDefault();const rules={enabled:[...document.querySelectorAll("[data-watch-rule]:checked")].map(input=>input.dataset.watchRule)};
  document.querySelectorAll("[data-watch-field]").forEach(input=>{rules[input.dataset.watchField]=Number(input.value)/(percentWatchFields.has(input.dataset.watchField)?100:1);});
  $("save-watch-button").disabled=true;
  try {
    await api('/api/watchlist',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset_id:$("watch-dataset-id").value,cost_price:$("watch-cost").value?Number($("watch-cost").value):null,cost_date:$("watch-cost").value?($("watch-cost-date").value||null):null,rules})});
    $("watch-dialog").close();await loadTools();toast("自选与规则已保存。");
  } catch(error) {$("watch-form-error").textContent=error.message;$("watch-form-error").hidden=false;}
  finally {$("save-watch-button").disabled=false;}
}

async function scanWatch() {
  $("scan-watch").disabled=true;
  try {
    const result=await api('/api/watchlist/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({as_of:$("tool-asof").value||null})});
    await loadTools();toast(`匹配 ${result.matched} 条规则，新增 ${result.new_alerts} 条预警。重复快照自动去重。`);
  } catch(error) {toolStatus(error.message);} finally {$("scan-watch").disabled=false;}
}

async function runStrategyScan(event) {
  event.preventDefault();$("run-scan").disabled=true;
  try {
    const result=await api('/api/scans',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({as_of:$("tool-asof").value||null,lookback:Number($("scan-lookback").value),volume_multiple:Number($("scan-volume").value),rsi_limit:Number($("scan-rsi").value),event_threshold:Number($("scan-event").value),benchmark_dataset_id:$("scan-benchmark").value||null})});
    $("scanner-summary").textContent=`已扫描 ${result.results.length} 份本地数据 · ${result.results.filter(row=>row.matched).length} 份满足全部条件 · 截止日 ${$("tool-asof").value||"各自最新记录"}`;
    $("strategy-results").innerHTML=result.results.map(row=>row.error?`<p class="tool-caption">${escapeHTML(row.dataset_name)}：${escapeHTML(row.error)}</p>`:`<details class="strategy-card"><summary><strong>${escapeHTML(row.dataset_name)}</strong><small>${escapeHTML(row.as_of)} · ${row.passed_count} / 6 · ${escapeHTML(row.status)} ＋</small></summary><ul>${row.conditions.map(c=>`<li><span class="${c.passed===null?"condition-unknown":c.passed?"condition-pass":"condition-fail"}">${c.passed===null?"? 未知":c.passed?"✓ 通过":"○ 未通过"}</span> · ${escapeHTML(c.label)}${c.value!==undefined?` · 当前 ${number(c.value,3)}`:""}${c.reference!==undefined?` / 参考 ${number(c.reference,3)}`:""}</li>`).join("")}</ul></details>`).join("");
    $("event-results").innerHTML=rowsHTML(result.results.filter(row=>row.event).map(row=>[row.dataset_name,row.as_of,percent(row.event.change,2,true),percent(row.event.threshold,0),row.event.streak]));
    toast("扫描完成。条件明细可展开查看。");
  }catch(error){toolStatus(error.message);}finally{$("run-scan").disabled=false;}
}

async function loadPaper() {
  if(state.view!=="workbench"||toolkit.active!=="portfolio")return;
  const sequence=++toolkit.paperSequence,query=toolQuery();
  try {
    const account=await api(`/api/paper${query}`);
    if(state.view!=="workbench"||toolkit.active!=="portfolio"||sequence!==toolkit.paperSequence||query!==toolQuery())return;
    toolkit.account=account;renderPaper();toolStatus(`模拟估值截至 ${account.as_of} · 使用本地日线收盘 · 仅记录虚拟资金成交`);requestAnimationFrame(drawToolCharts);
  } catch(error){toolStatus(error.message);}
}

function renderPaper() {
  const p=toolkit.account;if(!p)return;
  $("paper-equity").textContent=number(p.equity,2);$("paper-cash").textContent=number(p.cash,2);
  $("paper-return").textContent=`累计 ${percent(p.total_return,2,true)} · 初始虚拟资金 100000`;
  $("paper-exposure").textContent=percent(p.exposure,1);$("paper-drawdown").textContent=percent(p.drawdown,2);
  $("paper-date").textContent=`估值截止 ${p.as_of}`;
  $("paper-position-cap").textContent=`当前单标的上限 ${percent(p.risk_status.effective_position_cap,0)}`;
  $("paper-risk-notice").classList.toggle("blocked",!p.risk_status.can_buy);
  $("paper-risk-notice").textContent=p.risk_status.can_buy?`当前可提交模拟记录，买入仍需通过现金、单标的 ${percent(p.risk_status.effective_position_cap,0)}、总仓位 ${percent(p.risk.max_exposure,0)} 及持仓数量检查。`:p.risk_status.reasons.join("；");
  $("paper-positions").innerHTML=p.positions.map(row=>`<tr><td>${escapeHTML(row.dataset_name)}<br><small>${escapeHTML(row.quote_date)}</small></td><td>${row.quantity} / ${row.sellable}</td><td>${number(row.average_cost,4)}</td><td>${number(row.close,4)}</td><td class="${signedClass(row.unrealized_pnl)}">${number(row.unrealized_pnl,2)}<br>${percent(row.return,2,true)}</td><td>${percent(row.weight,1)}</td><td>${escapeHTML(row.flags.join("；")||"未触发参考线")}</td></tr>`).join("")||'<tr><td colspan="7">尚无模拟持仓。选择数据集和记录日后，可手动录入模拟买入。</td></tr>';
  const names=new Map(state.datasets.map(row=>[row.id,row.name]));
  $("paper-journal").innerHTML=p.trades.map(row=>`<tr><td>${escapeHTML(row.trade_date)}</td><td>${escapeHTML(names.get(row.dataset_id)||row.dataset_id)}</td><td>${row.side==="BUY"?"模拟买入":"模拟卖出"}</td><td>${row.quantity}</td><td>${number(row.price,4)}</td><td>${number(row.fee,4)}</td><td>${row.side==="SELL"?number(row.realized_pnl,2):"—"}</td></tr>`).join("")||'<tr><td colspan="7">还没有模拟成交日志。</td></tr>';
  $("paper-assumptions").innerHTML=p.assumptions.map(note=>`<li>${escapeHTML(note)}</li>`).join("");
  $("paper-risk-form").innerHTML=riskFields.map(([key,label,isPercent,min,max])=>`<label>${escapeHTML(label)}<input type="number" data-risk="${key}" value="${numberForInput(p.risk[key]*(isPercent?100:1))}" min="${min}" max="${max}" step="${['max_holdings','consecutive_loss_days','cooldown_sessions'].includes(key)?1:.1}" required></label>`).join("")+'<button type="submit" class="primary-button">保存模拟风控设置</button>';
}

async function recordPaperTrade(event) {
  event.preventDefault();$("record-paper-trade").disabled=true;
  try {
    await api('/api/paper/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset_id:$("tool-dataset").value,side:$("paper-side").value,quantity:Number($("paper-quantity").value),trade_date:$("paper-trade-date").value,client_id:crypto.randomUUID()})});
    await loadPaper();toast("模拟成交已记入本地账本。");
  }catch(error){toolStatus(error.message);toast(error.message);}finally{$("record-paper-trade").disabled=false;}
}

function bindToolControls() {
  document.querySelectorAll('[data-tool]').forEach(button=>button.addEventListener('click',()=>setTool(button.dataset.tool)));
  $("tool-dataset").addEventListener('change',()=>{$("dataset-select").value=$("tool-dataset").value;$("tool-asof").value="";$("paper-trade-date").value="";clearReport();updateDataset();loadTools();});
  $("tool-refresh").addEventListener('click',loadTools);$("tool-asof").addEventListener('change',()=>{$("paper-trade-date").value="";loadTools();});
  $("indicator-select").addEventListener('change',drawToolCharts);
  document.querySelectorAll('[data-candles]').forEach(button=>button.addEventListener('click',()=>{toolkit.candles=Number(button.dataset.candles);document.querySelectorAll('[data-candles]').forEach(item=>item.classList.toggle('active',item===button));drawToolCharts();}));
  $("add-watch").addEventListener('click',()=>openWatchDialog($("tool-dataset").value));$("watch-form").addEventListener('submit',saveWatch);
  $("scan-watch").addEventListener('click',scanWatch);$("alerts-unread").addEventListener('change',renderAlerts);
  $("watchlist-body").addEventListener('click',async event=>{
    const edit=event.target.closest('[data-edit-watch]'),remove=event.target.closest('[data-remove-watch]');
    if(edit)openWatchDialog(edit.dataset.editWatch);
    if(remove&&confirm('移出这个自选项？已保存的预警历史会保留。'))try{await api(`/api/watchlist/${encodeURIComponent(remove.dataset.removeWatch)}`,{method:'DELETE'});await loadTools();}catch(error){toolStatus(error.message);}
  });
  $("alerts-list").addEventListener('click',async event=>{const button=event.target.closest('[data-read-alert]');if(button)try{await api(`/api/alerts/${button.dataset.readAlert}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({acknowledged:true})});await loadTools();}catch(error){toolStatus(error.message);}});
  $("save-monitor").addEventListener('click',async()=>{try{await api('/api/monitor',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:$("monitor-enabled").checked,interval_seconds:Number($("monitor-interval").value)})});await loadTools();toast("本地检查设置已保存。");}catch(error){toolStatus(error.message);}});
  $("scan-form").addEventListener('submit',runStrategyScan);$("paper-trade-form").addEventListener('submit',recordPaperTrade);
  $("refresh-paper").addEventListener('click',loadPaper);
  $("paper-risk-form").addEventListener('submit',async event=>{event.preventDefault();const risk={};riskFields.forEach(([key,_,isPercent])=>{risk[key]=Number(document.querySelector(`[data-risk="${key}"]`).value)/(isPercent?100:1);});try{await api('/api/paper/risk',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(risk)});await loadPaper();toast("模拟风控设置已保存。");}catch(error){toolStatus(error.message);}});
  setInterval(()=>{if(state.view==="workbench"&&toolkit.active==="watchlist"&&!$("watch-dialog").open&&!document.activeElement?.matches('input,select'))loadTools();},15000);
}
