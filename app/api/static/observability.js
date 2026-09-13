"use strict";
const put = (tag, text) => { const node = document.createElement(tag); node.textContent = text; return node; };
fetch("/v1/observability").then(r => { if (!r.ok) throw Error("无法读取观测数据"); return r.json(); }).then(data => {
  for (const [label, value] of [["任务", data.sample_size], ["成功率", data.success_rate === null ? "—" : `${(100 * data.success_rate).toFixed(1)}%`], ["P95", `${data.latency.p95_ms?.toFixed(0) ?? "—"} ms`], ["未计价任务", data.unpriced_runs]]) {
    const card = put("p", label); card.append(put("strong", value)); document.querySelector("#stats").append(card);
  }
  document.querySelector("#stages").textContent = JSON.stringify({stages:data.stages, failures:data.failure_classes, retry_rate:data.retry_rate, fallback_rate:data.fallback_rate, known_estimated_cost:data.known_estimated_cost}, null, 2);
  for (const run of data.runs) {
    const row = document.createElement("tr");
    for (const value of [new Date(run.created_at).toLocaleString("zh-CN"), run.status, run.metrics.model_used ?? run.mode, `${run.metrics.duration_ms.toFixed(0)} ms`]) row.append(put("td", value));
    const cell = document.createElement("td"), button = put("button", "查看 Trace");
    button.addEventListener("click", async () => { try { const response = await fetch(`/v1/traces/${encodeURIComponent(run.trace_id)}`); if (!response.ok) throw Error("Trace 不可用"); document.querySelector("#trace").textContent = JSON.stringify(await response.json(), null, 2); document.querySelector("#detail").open = true; document.querySelector("#detail").scrollIntoView({behavior:"smooth"}); } catch(error) { document.querySelector("#error").textContent=error.message; } });
    cell.append(button); row.append(cell); document.querySelector("#runs").append(row);
  }
}).catch(error => { document.querySelector("#error").textContent = error.message; });
