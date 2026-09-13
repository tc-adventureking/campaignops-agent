# CampaignOps Agent

[English](README.md) · **简体中文**

查询广告投放指标、查阅业务口径、分析指标变化，在浏览器工作台查看数据和引用。接入真实模型进行 SQL 规划；只读校验、指标计算和候选原因分析由应用代码完成。

![诊断工作台：离线合成数据演示](docs/preview.png)

当前版本：**v0.2.0rc1**。主要使用流程需要配置真实模型服务。`demo` 模式用于离线演示和回归测试。

## 能做什么

- **口径问答**：检索版本化知识库，解释 CTR、CVR、CPA、ROAS 和归因规则，展示引用原文。
- **数据查询与诊断**：校验只读 SQL，查询 DuckDB 或 PostgreSQL，比较指标与渠道变化，给出带证据的候选原因。
- **可追溯工作台**：实时进度、Markdown 报告、来源查看、历史恢复、取消任务和报告下载，无需前端构建。
- **运行控制**：有界队列、持久化幂等、超时与有限重试、显式模型降级、可选 Redis 缓存，以及阶段耗时和 Trace 查看。
- **模拟预算操作**：提议、批准、执行与回滚使用独立沙盒账本，不连接真实广告平台。

## 快速启动：真实模型

准备 Python 3.12–3.14、uv、Git，以及支持工具调用的模型服务地址、模型 ID 和 API Key。从项目根目录安装 Youtu-Agent 路径所需的依赖：

```bash
uv sync --python 3.12 --frozen --extra youtu
```

仅在尚无 `.env` 时复制 [.env.example](.env.example) 为 `.env`；已有文件直接编辑并保留其他配置。填入账号实际支持的地址、模型 ID 和密钥：

```dotenv
AGENT_MODE=youtu
DATABASE_BACKEND=duckdb
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_NAME=your-enabled-model-id
MODEL_API_KEY=your-api-key
```

首次使用示例数据时执行以下命令，再启动工作台。命令也适用于 PowerShell。`seed_data` 会覆盖 `DATA_DIR` 下的开发数据库；已有数据需要保留时跳过它，重建前先停止 API。

```bash
uv run --no-sync python -m scripts.seed_data
uv run --no-sync python -m scripts.build_index
uv run --no-sync uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 **http://127.0.0.1:8000**；交互式接口文档为 **http://127.0.0.1:8000/docs**，运行记录页为 **http://127.0.0.1:8000/observability**。查询和诊断中的 SQL 规划会调用模型，按提供方规则消耗额度；口径检索、指标计算和预算审批等步骤由应用处理。

`youtu` 使用锁定版本的 Youtu-Agent；也可设置 `AGENT_MODE=openai`，通过基础依赖中的 HTTP 客户端直接调用兼容 Chat Completions 的接口。这是两条真实模型适配路径，`openai` 路径无需安装 `youtu` 依赖。缺失密钥或模型不可用时，模型请求返回错误；只有显式配置 `MODEL_FALLBACKS` 才尝试同服务的备用模型，不会自动退回 `demo`。配置变更后重启 API，兼容性探针见[测试文档](docs/testing.md#真实模型与缓存)。

`.env` 不进入 Git 或 Docker 镜像。模拟预算功能另需设置 `APPROVAL_API_KEY`，在提议卡片输入该口令；不要将模型密钥用作预算操作口令。

示例数据覆盖 **90 天、6 个 Campaign、4,320 行记录和 6 类注入异常**。默认“最近 7 天”按数据最新日期计算，为 2026-08-23 至 2026-08-29。

### 试试这些问题

界面和知识库以中文为主。可以用以下问题试运行已配置的模型，也可以用于离线演示：

| 场景 | 输入示例 |
| --- | --- |
| 了解口径 | `CTR 是如何计算的？` |
| 查询指标 | `查询 Campaign 3 最近7天的 CVR` |
| 分析异常 | `诊断 Campaign 6 最近7天渠道成本异常` |

输入后点击“开始分析”或按 Ctrl+Enter。来源链接可查看原文，报告可下载为 Markdown；浏览器历史通过运行 ID 恢复已有结果。

## 演示与测试：离线模式

不调用模型时使用 `demo`。在 `.env` 中设置以下两项，或通过当前进程的环境变量覆盖；保留其他已有配置：

```dotenv
AGENT_MODE=demo
DATABASE_BACKEND=duckdb
```

该模式不需要 API Key，只需基础依赖：`uv sync --python 3.12 --frozen`。使用上文的数据初始化与启动命令即可。无 `.env` 或环境覆盖时，代码默认也使用 `demo`；真实模型使用必须明确设置 `AGENT_MODE=youtu` 或 `openai`。离线结果用于检查规则和应用流程，不能用来评估真实模型能力。

## 数据库支持与扩展

| 需求 | 当前支持情况 |
| --- | --- |
| 本地分析数据库 | DuckDB；通过 `DATA_DIR` 指定目录，文件名为 `campaignops.duckdb` |
| 外部关系数据库 | PostgreSQL；配置 `DATABASE_BACKEND=postgres` 和只读 `POSTGRES_DSN` |
| 查询缓存 | 可选 Redis；它不替代分析数据库 |
| 多数据源动态切换、跨库联查 | 尚未实现；每个服务实例使用一个分析后端 |
| MySQL、ClickHouse 等其他引擎 | 尚无现成适配器，需要开发连接、方言和执行控制适配 |

在 Docker 之外使用 PostgreSQL 或 Redis 时安装 `storage` 依赖；使用 Youtu 时一起保留：`uv sync --python 3.12 --frozen --extra youtu --extra storage`。PostgreSQL 接入仍要求服务的 `DATA_DIR` 中保留数据元信息和运行状态目录。

接入自有广告数据需要匹配或适配当前表结构、字段白名单、指标定义和诊断逻辑，并同步数据版本与日期范围；当前并不自动识别任意业务库。新表或字段还需更新代码中的 schema 白名单，单改 `configs/schema.json` 不够。现有 PostgreSQL 迁移脚本用于初始化本项目的合成数据，不能当成任意业务库的导入工具。适配位置与验证要求见[数据库扩展说明](docs/architecture.md#数据库扩展边界)。

## Docker

需要 Docker 与 Compose 2.24+。完成上文的真实模型 `.env` 配置后执行；Compose 将模型配置传给 API，分析数据库默认使用 DuckDB：

```bash
docker compose up --build
```

未提供模型模式配置时，仍按代码默认值运行 `demo`。服务仅映射到本机 8000 端口；首次初始化数据，后续保留命名卷中的数据库和运行记录。可设置 `API_PORT` 更换端口。

PostgreSQL + Redis 模式在 Bash 中启动：

```bash
DATABASE_BACKEND=postgres REDIS_URL=redis://redis:6379/0 docker compose --profile storage up --build
```

PowerShell 可先设置 `$env:DATABASE_BACKEND="postgres"`、`$env:REDIS_URL="redis://redis:6379/0"`，再执行 `docker compose --profile storage up --build`。存储服务使用容器网络，不映射数据库端口。数据迁移、只读凭据和本地演示密码的范围见[存储配置](docs/architecture.md#postgresql-与-redis)。

## 验证与复现

完成离线数据初始化后，可检查评测任务和 HTTP 接口：

```bash
uv run --no-sync python -m scripts.evaluate --mode demo
uv run --no-sync python -m scripts.smoke_http --mode demo
```

以上命令使用离线模式。评分规则、报告导出和模型连通性检查见[测试文档](docs/testing.md)。生成报告与运行数据不进入 Git；自动检查的配置见 [CI 工作流](.github/workflows/ci.yml)。

## 项目结构与文档

| 路径 | 内容 |
| --- | --- |
| `app/` | API、工作台、Agent 编排、SQL 防护、分析与 Trace |
| `configs/` | 提示词、指标语义、查询示例与回归阈值 |
| `data/` | 数据库结构、合成种子、知识库与评测任务 |
| `scripts/` | 初始化、演示、评测、探针与验收脚本 |
| `tests/` | 单元、集成、安全与回归测试 |
| `docs/` | 使用说明、实现边界和验证方法 |

[架构与安全边界](docs/architecture.md) · [数据字典](docs/data-dictionary.md) · [API](docs/api.md) · [测试与复现](docs/testing.md)

## 使用边界

本项目面向本机合成数据演示。固定评测用于回归；满分不能证明对真实业务或未见问题具有同等能力。指标与候选原因来自确定性代码，置信度是规则分数，候选原因不构成因果证明；SQL 通过安全校验也不保证业务理解正确。

服务仅支持单进程、单 worker。分析接口尚无登录、租户隔离和配额管理，应监听本机；模拟预算账本独立于分析事实库和真实广告平台。生产部署还需要身份与数据隔离、权限管理和运维保障。

## 致谢

感谢 [腾讯 Youtu-Agent](https://github.com/TencentCloudADP/youtu-agent) 及以下开源项目的维护者与贡献者。本项目的模型框架、数据库访问、API 和报告渲染建立在这些项目的工作之上。

| 上游项目 | 本项目中的用途 | 接入路径 |
| --- | --- | --- |
| [Youtu-Agent](https://github.com/TencentCloudADP/youtu-agent) | 使用 `SimpleAgent`、`AgentConfig` 进行真实模型 SQL 规划 | [app/agent/runners.py](app/agent/runners.py) |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) / [OpenAI Python SDK](https://github.com/openai/openai-python) | Youtu 路径中的工具注册、模型适配与异步客户端 | [app/agent/runners.py](app/agent/runners.py) |
| [HTTPX](https://github.com/encode/httpx) | 直接模型适配器与模型探针的 HTTP 客户端 | [app/agent/runners.py](app/agent/runners.py)、[scripts/model_probe.py](scripts/model_probe.py) |
| [DuckDB](https://github.com/duckdb/duckdb) | 本地分析数据库与合成数据初始化 | [app/tools/database.py](app/tools/database.py)、[scripts/seed_data.py](scripts/seed_data.py) |
| [SQLGlot](https://github.com/tobymao/sqlglot) | SQL 解析、白名单校验与方言转换 | [app/guardrails/sql.py](app/guardrails/sql.py)、[app/tools/database.py](app/tools/database.py) |
| [FastAPI](https://github.com/fastapi/fastapi) | HTTP API 与 SSE 接口 | [app/api/main.py](app/api/main.py) |
| [Psycopg](https://github.com/psycopg/psycopg) / [redis-py](https://github.com/redis/redis-py) | PostgreSQL 连接与 Redis 查询缓存 | [app/tools/database.py](app/tools/database.py)、[app/tools/cache.py](app/tools/cache.py) |
| [markdown-it-py](https://github.com/executablebooks/markdown-it-py) | Markdown 报告渲染 | [app/api/presentation.py](app/api/presentation.py) |

Youtu-Agent 固定使用[提交 c2caa539](https://github.com/TencentCloudADP/youtu-agent/tree/c2caa539f4c95ae1c39ed24dc8a99cb3651e1d5d)。依赖声明与锁定版本见 [pyproject.toml](pyproject.toml)、[uv.lock](uv.lock)，许可证归属见[第三方说明](THIRD_PARTY_NOTICES.md)。

## 许可证

使用 [MIT 许可证](LICENSE)。依赖归属见[第三方说明](THIRD_PARTY_NOTICES.md)。
