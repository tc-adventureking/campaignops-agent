# API 使用

启动后访问 `/docs` 交互调试，`/openapi.json` 为接口契约。`/` 提供响应式诊断工作台，资源位于 `/static/`。所有示例从项目根目录运行。

`GET /v1/workspace` 返回运行模式、合成数据范围和可阅读的知识片段目录，不返回密钥或模型连接配置。`POST /v1/markdown` 接收 `{"markdown":"# 标题"}`，返回 `{"html":"<h1>标题</h1>\n"}`；最大 100,000 字符，原始 HTML 不执行，危险链接协议被拒绝。支持表格、列表、引用、围栏代码与删除线。报告保留 Markdown 原文供导出，未改变运行结果契约。

```bash
curl http://127.0.0.1:8000/health/ready
curl -X POST http://127.0.0.1:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"question":"诊断 Campaign 3 最近7天 CVR 下滑的原因"}'
# 将返回的 run_id 替换到以下 URL。
curl -N http://127.0.0.1:8000/v1/runs/RUN_ID/events
curl http://127.0.0.1:8000/v1/runs/RUN_ID
curl -N -H 'Last-Event-ID: 3' http://127.0.0.1:8000/v1/runs/RUN_ID/events
curl http://127.0.0.1:8000/v1/knowledge/metrics.cvr
```

创建任务返回 202（queued）。请求允许 `question`（2–2000 字符）、可选 `start_date/end_date`（必须成对）、可选 `campaign_id`（1–6）。日期字段优先于问题中的日期。SSE 的 ID 是单个任务内递增序号，事件包含 run_id、trace_id、span_id、时间及结构化 data；支持 status/tool/model/context/evidence/result/error 等事件。浏览器 EventSource 自动发送 Last-Event-ID 重连。

| HTTP | 语义 |
| --- | --- |
| 400 | 无效事件游标、业务参数 |
| 404 | run/chunk/trace 不存在 |
| 409 | 游标超出范围或非法状态转换 |
| 422 | Pydantic 请求校验失败、SQL 拒绝 |
| 429 | 执行与等待队列总上限 |
| 500 | 系统或未分类执行错误 |
| 502 | 模型不可用、无工具能力、响应无效 |
| 504 | 模型或查询超时 |
| 503 | readiness 依赖检查未通过 |

任务创建后的异步错误通过 GET 结果的 `status=failed/error` 与 SSE error 事件返回；不会把已返回的 202 改成 502/504。错误含 code/message/retryable；内部路径、上游响应体和密钥不会返回。健康探针不会调用付费 API；readiness 检查数据库可查询、manifest、知识和模型配置，`model_network_checked=false` 明确指出未测试远端。

成功运行的 `metrics` 包含 `input_tokens`、`output_tokens`、`cache_hit_tokens` 和 `cache_miss_tokens`。缓存字段采用提供方返回的 usage；兼容接口只提供 `prompt_tokens_details.cached_tokens` 时，用总输入减去命中数得到未命中数。提供方未报告、离线 demo 或未调用模型的任务返回 null，不能把它当作零命中。模型 Trace 事件也保留这些字段；旧运行记录可以继续读取。

Trace 导出：

```bash
uv run python -m scripts.export_trace TRACE_ID --output artifacts/trace.jsonl
```

预算修改类请求返回终态 `approval_required`，`approval.executable=false`；模拟执行使用下面的独立审批接口。普通分析成功时为 `succeeded`。部署边界为本机、合成数据、单 worker，公网服务的鉴权和多租户隔离属于后续阶段。

## v0.2 新增接口

`POST /v1/runs` 接受 `Idempotency-Key` 请求头（8–128 位字母、数字或 `_.:-`）。同键同参数返回原运行，同键异参返回 409；队列满返回 429。`POST /v1/runs/{run_id}/cancel` 取消排队/执行任务，重复取消返回已有终态。

`GET /v1/observability?limit=200` 返回近期任务、阶段 P50/P95、错误分类、重试/降级/拒绝率。`GET /v1/traces/{trace_id}` 导出完整事件。`/observability` 为人类可读页面。`RunResult.metrics` 新增 `model_used`、`retry_count`、`fallback_count`、`attempts`、`usage_complete`、`price_version`、`cost_currency`；Trace Span 有 `parent_span_id`，评测运行有 `experiment_id`。

模拟预算接口全部要求 `X-Operator-Key`，其值为本地单独设置的 `APPROVAL_API_KEY`，不能用模型密钥代替：

| 接口 | 请求与效果 |
| --- | --- |
| `POST /v1/approvals?run_id=…` | `{"campaign_id":3,"new_budget":1200}`；返回摘要、哈希、有效期、操作者。run_id 可省略，提供时须指向待审批任务 |
| `GET /v1/approvals/{id}` | 查询状态；不返回一次性令牌 |
| `POST /v1/approvals/{id}/decision` | `{"decision":"approve或reject","parameters_hash":"提议中的哈希"}`；批准时返回一次 `execution_token` |
| `POST /v1/approvals/{id}/execute` | `{"execution_token":"…","parameters":{"campaign_id":3,"new_budget":1200}}`；验证后只更新模拟账本 |
| `POST /v1/approvals/{id}/rollback` | 已执行提议生成回滚提议，须再次批准与执行 |
| `GET /v1/approvals/{id}/audit` | 按序查询审批审计记录 |

分析 Run 的 `approval_required` 仍为终态且 `executable=false`；执行发生在独立审批状态机中。403 表示操作者或执行凭证无效，409 表示审批状态、参数、有效期或预算修订冲突。令牌只保留在当前页面内存，不写浏览器存储；刷新丢失令牌后需重新提议，不能重新取回旧令牌。
