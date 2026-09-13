"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { busy: false, run: null, id: null, revision: 0, source: null, timer: null, poll: null, finishing: false, workspace: null, question: "", context: null };
  const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
  const historyKey = "campaignops.history.v1";
  const terminal = new Set(["succeeded", "failed", "approval_required", "cancelled"]);
  const validId = (value) => typeof value === "string" && /^[0-9a-f-]{36}$/i.test(value);
  let toastTimer;
  let dialogRevision = 0;

  function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, { ...options, signal: controller.signal, headers: { "Content-Type": "application/json", ...options.headers } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || `请求未完成（${response.status}）`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("连接等待超时，请检查本地服务后重试。");
      if (error instanceof TypeError) throw new Error("暂时无法连接本地服务，请检查服务是否正在运行。");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $("#toast").textContent = message;
    $("#toast").hidden = false;
    toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 3000);
  }

  function readHistory() {
    try {
      const items = JSON.parse(localStorage.getItem(historyKey) || "[]");
      return Array.isArray(items) ? items.filter(item => validId(item?.id) && typeof item.question === "string").slice(0, 8) : [];
    } catch { return []; }
  }

  function saveHistory(run) {
    const item = { id: run.run_id, question: state.question.slice(0, 2000), time: run.created_at };
    try { localStorage.setItem(historyKey, JSON.stringify([item, ...readHistory().filter(row => row.id !== item.id)].slice(0, 8))); } catch { /* History remains optional in private browsing. */ }
    renderHistory();
  }

  function historyButtons(target) {
    target.replaceChildren();
    const items = readHistory();
    if (!items.length) target.append(element("p", "history-empty", "暂无分析记录"));
    for (const item of items) {
      const button = element("button", `history-item${item.id === state.id ? " selected" : ""}`);
      button.title = item.question;
      button.disabled = state.busy;
      button.append(icon("file"), element("span", "", item.question));
      button.addEventListener("click", () => { $("#content-dialog").close(); void loadRun(item.id, item.question); });
      target.append(button);
    }
  }

  function renderHistory() { historyButtons($("#history-list")); }

  function setBusy(busy) {
    state.busy = busy;
    $("#submit").disabled = busy;
    $("#question").disabled = busy;
    $("#new-run").disabled = busy;
    $$(".scenario, .history-item").forEach(button => { button.disabled = busy; });
    $("#submit-label").textContent = busy ? "分析中…" : "开始分析";
    $("#result-section").setAttribute("aria-busy", String(busy));
  }

  function stopListening() {
    state.source?.close();
    state.source = null;
    clearInterval(state.timer);
    clearInterval(state.poll);
    state.timer = state.poll = null;
  }

  function selectTab(name, focus = false) {
    $$(".report-tab").forEach(tab => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      $(`#panel-${tab.dataset.tab}`).hidden = !active;
      if (active && focus) tab.focus();
    });
  }

  function reset() {
    stopListening();
    state.revision += 1;
    state.id = state.run = state.context = null;
    state.finishing = false;
    setBusy(false);
    $("#result-section").hidden = $("#progress-panel").hidden = $("#error-panel").hidden = true;
    $("#empty-state").hidden = false;
    $("#metric-cards").replaceChildren();
    $("#report-content").replaceChildren();
    history.replaceState(null, "", location.pathname);
    renderHistory();
  }

  function updateCount() { $("#character-count").textContent = `${$("#question").value.length} / 2000`; }

  function progress(step, message) {
    $$("#progress-steps li").forEach((item, index) => {
      item.classList.toggle("current", index === step);
      item.classList.toggle("done", index < step);
      item.setAttribute("aria-current", index === step ? "step" : "false");
      $("span", item).textContent = index < step ? "✓" : String(index + 1);
    });
    $("#progress-message").textContent = message;
  }

  function begin(question) {
    reset();
    state.question = question;
    setBusy(true);
    $("#empty-state").hidden = true;
    $("#progress-panel").hidden = false;
    progress(0, "正在读取业务口径。");
    const start = performance.now();
    $("#elapsed").textContent = "0.0s";
    state.timer = setInterval(() => { $("#elapsed").textContent = `${((performance.now() - start) / 1000).toFixed(1)}s`; }, 100);
  }

  function showError(message, retry = true) {
    stopListening();
    setBusy(false);
    $("#progress-panel").hidden = true;
    $("#error-panel").hidden = false;
    $("#error-message").textContent = message;
    $("#retry-button").hidden = !retry;
  }

  async function submit(event) {
    event?.preventDefault();
    if (state.busy || !$("#analysis-form").reportValidity()) return;
    const question = $("#question").value.trim();
    if (question.length < 2) { toast("请描述你想分析的问题，至少两个字符。"); return; }
    begin(question);
    try {
      const body = JSON.stringify({ question });
      let pending;
      try { pending = JSON.parse(sessionStorage.getItem("campaignops.pending") || "null"); } catch { /* Storage is optional. */ }
      if (!pending || pending.body !== body) pending = { body, key: crypto.randomUUID() };
      try { sessionStorage.setItem("campaignops.pending", JSON.stringify(pending)); } catch { /* Idempotency still protects this request. */ }
      const run = await api("/v1/runs", { method: "POST", body, headers: { "Idempotency-Key": pending.key } });
      try { sessionStorage.removeItem("campaignops.pending"); } catch { /* no-op */ }
      state.id = run.run_id;
      history.replaceState(null, "", `?run=${encodeURIComponent(state.id)}`);
      saveHistory(run);
      if (terminal.has(run.status)) await finish(run);
      else watch(run.run_id);
    } catch (error) { showError(error.message); }
  }

  function approvalForm(card, run) {
    card.append(element("h3", "", "模拟预算沙盒"), element("p", "", "先填写并审阅提议，再批准、执行。所有变化只保存在模拟账本中，回滚也需重新审批。"));
    const campaign = element("select"), amount = element("input"), key = element("input");
    campaign.setAttribute("aria-label", "模拟 Campaign");
    for (let i=1; i<=6; i++) { const option = element("option", "", `Campaign ${i}`); option.value = i; campaign.append(option); }
    amount.type = "number"; amount.min = "0"; amount.max = "1000000"; amount.step = "0.01"; amount.value = "1200"; amount.setAttribute("aria-label", "模拟日预算");
    key.type = "password"; key.autocomplete = "off"; key.placeholder = "审批口令"; key.setAttribute("aria-label", "审批口令");
    const create = element("button", "quiet-button", "生成模拟提议"), review = element("div");
    create.type = "button";
    create.addEventListener("click", async () => {
      if (!key.value || !amount.reportValidity()) { toast("请填写审批口令和有效预算。"); return; }
      const headers = {"X-Operator-Key": key.value};
      create.disabled = true;
      async function show(proposal) {
        campaign.value = String(proposal.parameters.campaign_id);
        amount.value = String(proposal.parameters.new_budget);
        campaign.disabled = amount.disabled = true;
        review.replaceChildren(element("p", "", proposal.summary), element("p", "", `操作者：${proposal.actor} · 有效至 ${new Date(proposal.expires_at).toLocaleTimeString()}`));
        const edit = element("button", "quiet-button", "修改参数并重新提议");
        edit.addEventListener("click", () => { campaign.disabled=amount.disabled=false; review.replaceChildren(); create.disabled=false; });
        review.append(edit);
        const approve = element("button", "quiet-button", "批准此提议"), reject = element("button", "quiet-button", "拒绝");
        review.append(approve, reject);
        for (const [button, decision] of [[approve, "approve"], [reject, "reject"]]) button.addEventListener("click", async () => {
          approve.disabled = reject.disabled = true;
          try {
            const response = await api(`/v1/approvals/${proposal.id}/decision`, {method:"POST",headers,body:JSON.stringify({decision, parameters_hash:proposal.parameters_hash})});
            if (decision === "reject") { review.append(element("p", "", "提议已拒绝，未执行。")); return; }
            const execute = element("button", "primary-button", "执行已批准的模拟变更"); review.append(execute);
            execute.addEventListener("click", async () => {
              execute.disabled = true;
              try {
                await api(`/v1/approvals/${proposal.id}/execute`, {method:"POST",headers,body:JSON.stringify({execution_token:response.execution_token,parameters:proposal.parameters})});
                response.execution_token = null;
                review.append(element("p", "", "模拟预算已更新；真实投放未受影响。"));
                const rollback = element("button", "quiet-button", "提议回滚"), audit = element("button", "quiet-button", "查看审批记录"); review.append(rollback, audit);
                rollback.addEventListener("click", async () => { rollback.disabled=true; try { await show(await api(`/v1/approvals/${proposal.id}/rollback`,{method:"POST",headers})); } catch(error){toast(error.message);} });
                audit.addEventListener("click", async () => { try { const rows=await api(`/v1/approvals/${proposal.id}/audit`,{headers}); review.append(element("pre", "", rows.map(row=>`${row.timestamp}  ${row.actor}  ${row.event}`).join("\n"))); } catch(error){toast(error.message);} });
              } catch(error) { toast(error.message); }
            });
          } catch(error) { toast(error.message); }
        });
      }
      try { await show(await api(`/v1/approvals?run_id=${encodeURIComponent(run.run_id)}`, {method:"POST",headers,body:JSON.stringify({campaign_id:Number(campaign.value),new_budget:Number(amount.value)})})); }
      catch(error) { toast(error.message); }
      finally { create.disabled=false; }
    });
    card.append(campaign, amount, key, create, review);
  }

  function watch(id) {
    const source = new EventSource(`/v1/runs/${encodeURIComponent(id)}/events`);
    state.source = source;
    const parse = event => JSON.parse(event.data).data;
    source.addEventListener("context", event => {
      if (state.id !== id) return;
      state.context = parse(event);
      progress(0, `分析范围：${state.context.start_date} 至 ${state.context.end_date}，正在检索业务口径。`);
    });
    source.addEventListener("model", event => {
      if (state.id !== id) return;
      progress(1, parse(event).status === "started" ? "正在结合业务口径规划只读查询。" : "查询计划已生成，正在验证可执行性。");
    });
    source.addEventListener("tool", event => {
      if (state.id !== id) return;
      const data = parse(event);
      const steps = { retrieve: [0, "正在检索指标公式与业务规则。"], sql: [2, "正在读取投放数据，并保留查询证据。"], diagnostic_sql: [2, "正在补充诊断所需的数据。"], analyze: [3, "正在对比两期指标，整理原因与建议。"] };
      if (steps[data.name]) progress(...steps[data.name]);
    });
    source.addEventListener("result", event => { if (state.id === id) void finish(parse(event)); });
    source.addEventListener("error", event => {
      if (state.id !== id || state.finishing) return;
      if (event.data) void finish(parse(event));
      else $("#progress-message").textContent = "连接暂时中断，正在恢复分析进度；任务会在后台继续。";
    });
    // Read-only polling also handles a lost final SSE event. Never re-submit a paid model request.
    let polling = false;
    state.poll = setInterval(async () => {
      if (polling || state.finishing || state.id !== id) return;
      polling = true;
      try {
        const run = await api(`/v1/runs/${encodeURIComponent(id)}`);
        if (state.id === id && terminal.has(run.status)) await finish(run);
      } catch { /* EventSource and this poll will reconnect. */ }
      finally { polling = false; }
    }, 3000);
  }

  async function loadRun(id, question = "历史分析") {
    if (state.busy || !validId(id)) return;
    $("#question").value = question;
    updateCount();
    begin(question);
    state.id = id;
    history.replaceState(null, "", `?run=${encodeURIComponent(id)}`);
    renderHistory();
    try {
      const run = await api(`/v1/runs/${encodeURIComponent(id)}`);
      if (terminal.has(run.status)) await finish(run);
      else watch(id);
    } catch (error) { showError(`无法读取这份分析：${error.message}`, false); }
  }

  function enhanceMarkdown(target) {
    $$("table", target).forEach(table => {
      const wrapper = element("div", "table-scroll");
      wrapper.tabIndex = 0;
      wrapper.setAttribute("role", "region");
      wrapper.setAttribute("aria-label", "数据表，可横向滚动查看");
      table.before(wrapper);
      wrapper.append(table);
    });
    $$("a", target).forEach(link => {
      let url;
      try { url = new URL(link.getAttribute("href"), location.origin); }
      catch { link.removeAttribute("href"); return; }
      if (url.origin === location.origin && url.pathname.startsWith("/v1/knowledge/")) {
        link.addEventListener("click", event => { event.preventDefault(); void openSource(decodeURIComponent(url.pathname.slice("/v1/knowledge/".length))); });
      } else if (["http:", "https:"].includes(url.protocol)) {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
    });
  }

  async function markdown(target, source, isCurrent = () => true) {
    try {
      const result = await api("/v1/markdown", { method: "POST", body: JSON.stringify({ markdown: source }) });
      if (!isCurrent()) return;
      // The local renderer disables raw HTML, unsafe protocols and images before HTML enters the DOM.
      target.innerHTML = result.html;
      enhanceMarkdown(target);
    } catch {
      if (!isCurrent()) return;
      target.replaceChildren(element("p", "status-description", "排版暂时不可用，以下为原始 Markdown；可复制或下载。"), element("pre", "raw-fallback", source));
    }
  }

  function metricCards(analysis) {
    const container = $("#metric-cards");
    container.replaceChildren();
    container.hidden = !analysis;
    if (!analysis) return;
    const metrics = [["cvr", "转化率 CVR", "percent"], ["conversions", "转化量", "count"], ["cpc", "点击成本 CPC", "money"], ["spend", "广告消耗", "money"]];
    for (const [key, label, type] of metrics) {
      const card = element("div", "metric-card");
      const value = analysis.current[key];
      const display = value == null ? "—" : type === "percent" ? `${(value * 100).toFixed(2)}%` : type === "money" ? `¥${number.format(value)}` : number.format(value);
      card.append(element("div", "metric-label", label), element("div", "metric-value", display));
      const change = analysis.changes[key]?.relative;
      const delta = element("div", "metric-change");
      if (change == null) delta.textContent = "暂无可比上期";
      else {
        const good = key === "cpc" ? change < 0 : change > 0;
        const tone = key === "spend" || change === 0 ? "" : good ? "positive" : "negative";
        delta.append(element("b", tone, `${change > 0 ? "↑" : change < 0 ? "↓" : ""} ${Math.abs(change * 100).toFixed(1)}%`), document.createTextNode("较上期"));
      }
      card.append(delta);
      container.append(card);
    }
  }

  function evidencePanel(answer) {
    const target = $("#panel-evidence");
    target.replaceChildren(element("p", "", "文档解释指标口径，查询结果提供数据依据。点击来源可阅读原文。"));
    const items = [...(answer?.evidence || []), ...(answer?.citations || [])];
    const seen = new Set();
    let count = 0;
    for (const item of items) {
      const key = `${item.kind}:${item.chunk_id || item.query_hash}:${item.version || ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      count += 1;
      const card = element("section", "evidence-card");
      card.append(element("div", "evidence-label", item.kind === "query" ? "数据查询" : "业务口径"));
      if (item.kind === "query") {
        card.append(element("h3", "", `只读查询 · ${item.row_count ?? "—"} 行结果`), element("p", "", `查询指纹：${item.query_hash}`));
        const details = element("details");
        details.append(element("summary", "", "查看原始结果摘要"), element("pre", "", item.summary));
        card.append(details);
      } else {
        const title = item.summary.split("\n", 1)[0].replace(/^##\s*/, "").replace(/\s*\{#[^}]+\}/g, "");
        card.append(element("h3", "", title || item.chunk_id), element("p", "", `版本 ${item.version} · ${item.chunk_id}`));
        const button = element("button", "text-button", "阅读来源 →");
        button.addEventListener("click", () => { void openSource(item.chunk_id); });
        card.append(button);
      }
      target.append(card);
    }
    if (!count) target.append(element("p", "", "这次请求没有产生查询或文档证据。"));
    $("#evidence-count").textContent = count;
  }

  function detailsPanel(run) {
    const target = $("#panel-details");
    const usage = run.metrics || {};
    target.replaceChildren(element("p", "", "运行标识用于定位记录；Token 统计来自模型接口，未报告的字段显示为“未提供”。"));
    const list = element("dl", "details-grid");
    const hit = usage.cache_hit_tokens;
    const items = [["运行模式", run.mode === "demo" ? "离线规则演示" : "真实模型分析"], ["完成状态", run.status], ["运行 ID", run.run_id], ["Trace ID", run.trace_id], ["输入 Token", usage.input_tokens], ["输出 Token", usage.output_tokens], ["缓存命中 Token", hit], ["缓存未命中 Token", usage.cache_miss_tokens], ["输入缓存命中率", hit != null && usage.input_tokens ? `${(hit / usage.input_tokens * 100).toFixed(1)}%` : null], ["运行耗时", `${((usage.duration_ms || 0) / 1000).toFixed(2)} 秒`]];
    for (const [label, value] of items) {
      const row = element("div", "detail-item");
      row.append(element("dt", "", label), element("dd", "", value == null ? "未提供" : String(value)));
      list.append(row);
    }
    target.append(list);
  }

  async function finish(run) {
    if (state.finishing || state.id !== run.run_id) return;
    state.finishing = true;
    stopListening();
    state.run = run;
    const revision = state.revision;
    if (run.status === "failed" || run.status === "cancelled") {
      showError(run.error?.message || "这次分析已中断，请重新提交问题。");
      return;
    }
    try {
      progress(3, "正在整理结果。");
      const approval = run.status === "approval_required";
      $("#result-title").textContent = approval ? "投放变更提议" : "分析结果";
      $("#result-question").textContent = state.question;
      $("#result-badge").classList.toggle("approval", approval);
      $("#result-badge span").textContent = approval ? "待批准" : "已完成";
      $("#copy-report").disabled = $("#download-report").disabled = approval || !run.answer?.markdown;
      metricCards(run.answer?.analysis);
      evidencePanel(run.answer);
      detailsPanel(run);
      const content = $("#report-content");
      if (approval) {
        const card = element("div", "approval-message");
        card.append(element("h2", "", "投放变更提议"), element("p", "", run.approval?.summary), element("p", "", run.approval?.reason), element("span", "approval-state", "提议已记录 · 未执行投放变更"));
        content.replaceChildren(card);
        approvalForm(card, run);
      } else await markdown(content, run.answer?.markdown || run.answer?.conclusion || "暂无可展示的报告。", () => revision === state.revision);
      if (revision !== state.revision) return;
      selectTab("report");
      $("#result-section").hidden = false;
      $("#progress-panel").hidden = true;
      $("#report-footer-text").textContent = run.mode === "demo" ? "合成数据 · 离线规则演示" : "合成数据 · 真实模型分析";
      $("#report-duration").textContent = `${((run.metrics?.duration_ms || 0) / 1000).toFixed(2)}s`;
      $("#result-title").focus({ preventScroll: true });
    } catch (error) { showError(`报告展示失败：${error.message}`, false); }
    finally { if (revision === state.revision) { setBusy(false); renderHistory(); } }
  }

  function dialog(title) {
    dialogRevision += 1;
    $("#dialog-title").textContent = title;
    $("#dialog-body").replaceChildren();
    if (!$("#content-dialog").open) $("#content-dialog").showModal();
    return dialogRevision;
  }

  async function openSource(id) {
    const revision = dialog("业务口径来源");
    const body = $("#dialog-body");
    body.append(element("p", "section-loading", "正在读取来源…"));
    try {
      const source = await api(`/v1/knowledge/${encodeURIComponent(id)}`);
      if (revision !== dialogRevision) return;
      const intro = element("div", "source-intro", `${source.chunk_id} · 版本 ${source.version} · 原文第 ${source.line_start}–${source.line_end} 行`);
      const article = element("article", "markdown-body");
      body.replaceChildren(intro, article);
      await markdown(article, source.summary.replace(/\s*\{#[^}]+\}/g, ""), () => revision === dialogRevision);
    } catch (error) { if (revision === dialogRevision) body.replaceChildren(element("p", "", error.message)); }
  }

  function showKnowledge() {
    dialog("指标知识库");
    const target = $("#dialog-body");
    if (!state.workspace) { target.append(element("p", "", "知识库暂时不可用，请检查本地服务后刷新页面。")); return; }
    const list = element("div", "knowledge-list");
    for (const source of state.workspace.knowledge) {
      const button = element("button", "knowledge-item", source.title);
      button.append(element("small", "", `${source.chunk_id} · v${source.version}`));
      button.addEventListener("click", () => { void openSource(source.chunk_id); });
      list.append(button);
    }
    target.append(list);
  }

  function showHelp() {
    dialog("使用指南");
    const list = element("ol", "guide-list");
    const tips = [["提交问题", "写明指标、Campaign 和日期，或选择示例问题。点击“开始分析”才会提交，真实模式会调用已配置的模型。"], ["阅读报告与证据", "指标卡片展示变化，报告解释候选原因。表格可以横向滚动，点击口径链接即可阅读来源。"], ["保存与继续分析", "可复制或下载 Markdown 报告。最近 8 次分析保存在当前浏览器，可重新打开；刷新正在分析的页面不会重复提交请求。"], ["数据与操作范围", "这里使用合成广告数据，默认日期以数据最新一天为准。诊断分数不代表因果概率。预算提议经批准后仅修改模拟账本。"]];
    for (const [title, detail] of tips) {
      const item = element("li");
      item.append(element("strong", "", title), element("p", "", detail));
      list.append(item);
    }
    $("#dialog-body").append(list);
    const knowledge = element("button", "primary-button", "浏览指标知识库");
    knowledge.addEventListener("click", showKnowledge);
    $("#dialog-body").append(knowledge);
  }

  async function loadWorkspace() {
    try {
      const [workspace, health] = await Promise.all([api("/v1/workspace"), api("/health/ready")]);
      state.workspace = workspace;
      $("#connection").classList.add("ready");
      $("#connection span").textContent = health.status === "ready" ? "服务已就绪" : "服务待就绪";
      $("#mode-badge").textContent = workspace.mode === "demo" ? "离线演示" : "真实模型已配置";
      $("#knowledge-count").textContent = workspace.knowledge.length;
      $("#data-range").textContent = `${workspace.data.start_date} — ${workspace.data.end_date}`;
      $("#data-days").textContent = number.format(workspace.data.days);
      $("#data-rows").textContent = number.format(workspace.data.rows);
      $("#scope-note").textContent = `“最近 7 天”以 ${workspace.data.end_date} 为截止日。${workspace.data.currency} · ${workspace.data.timezone}`;
    } catch (error) {
      $("#connection").classList.add("unavailable");
      $("#connection span").textContent = "服务暂不可用";
      $("#data-range").textContent = "暂未读取到数据范围";
      $("#scope-note").textContent = error.message;
    }
  }

  $("#analysis-form").addEventListener("submit", event => { void submit(event); });
  $("#cancel-run").addEventListener("click", async () => {
    if (!state.id) return;
    $("#cancel-run").disabled = true;
    try { const run = await api(`/v1/runs/${state.id}/cancel`, {method:"POST"}); await finish(run); }
    catch(error) { toast(error.message); }
    finally { $("#cancel-run").disabled = false; }
  });
  $("#question").addEventListener("input", updateCount);
  $("#question").addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void submit(); }
  });
  $("#new-run").addEventListener("click", () => { reset(); $("#question").value = ""; updateCount(); $("#question").focus(); });
  $("#workspace-nav").addEventListener("click", () => { $("#question").scrollIntoView({ block: "center" }); $("#question").focus(); });
  $("#knowledge-nav").addEventListener("click", showKnowledge);
  $("#history-button").addEventListener("click", () => {
    dialog("最近分析");
    historyButtons($("#dialog-body"));
  });
  $("#help-button").addEventListener("click", showHelp);
  $("#close-dialog").addEventListener("click", () => { $("#content-dialog").close(); });
  $("#content-dialog").addEventListener("close", () => { dialogRevision += 1; });
  $("#content-dialog").addEventListener("click", event => {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (event.target === event.currentTarget && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) event.currentTarget.close();
  });
  $$(".scenario").forEach(button => button.addEventListener("click", () => {
    $("#question").value = button.dataset.question;
    updateCount();
    $("#question").focus();
    $("#question").scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
  }));
  $("#retry-button").addEventListener("click", () => { void submit(); });
  $$(".report-tab").forEach(tab => {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
    tab.addEventListener("keydown", event => {
      const tabs = $$(".report-tab");
      const index = tabs.indexOf(tab);
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
      if (next >= 0) { event.preventDefault(); selectTab(tabs[next].dataset.tab, true); }
    });
  });
  $("#copy-report").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.run.answer.markdown); toast("Markdown 已复制"); }
    catch { toast("剪贴板不可用，请使用旁边的下载按钮。"); }
  });
  $("#download-report").addEventListener("click", () => {
    if (!state.run?.answer?.markdown) return;
    const url = URL.createObjectURL(new Blob([state.run.answer.markdown], { type: "text/markdown;charset=utf-8" }));
    const link = element("a");
    link.href = url;
    link.download = `CampaignOps-${state.run.run_id.slice(0, 8)}.md`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("报告已开始下载");
  });
  if (/Mac|iPhone|iPad/.test(navigator.platform)) $("#shortcut-modifier").textContent = "⌘";
  $$("svg.icon").forEach(svg => svg.setAttribute("aria-hidden", "true"));
  updateCount();
  renderHistory();
  void loadWorkspace();
  const initialRun = new URLSearchParams(location.search).get("run");
  if (validId(initialRun)) void loadRun(initialRun, readHistory().find(row => row.id === initialRun)?.question || "历史分析");
})();
