# Third-party notices

CampaignOps Agent's original code is MIT licensed. Dependencies retain their own licenses; this project's MIT license does not relicense them. Dependencies are installed from the pinned `uv.lock`; their distributions include the full notices and license texts.

| Component | License in installed distribution |
| --- | --- |
| [Tencent Youtu-Agent](https://github.com/TencentCloudADP/youtu-agent/tree/c2caa539f4c95ae1c39ed24dc8a99cb3651e1d5d), pinned commit `c2caa539f4c95ae1c39ed24dc8a99cb3651e1d5d` | MIT |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | MIT |
| [OpenAI Python SDK](https://github.com/openai/openai-python) | Apache-2.0 |
| [DuckDB](https://github.com/duckdb/duckdb), [FastAPI](https://github.com/fastapi/fastapi), [SQLGlot](https://github.com/tobymao/sqlglot) | MIT |
| [markdown-it-py](https://github.com/executablebooks/markdown-it-py) / markdown-it | MIT |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause |
| [psycopg / psycopg-binary](https://github.com/psycopg/psycopg) | LGPL-3.0-only |
| [redis-py](https://github.com/redis/redis-py) | MIT |

The optional PostgreSQL and Redis server images are separate upstream distributions with their own licensing and bundled notices. When distributing a container or a dependency bundle, retain all installed third-party license/source notices and satisfy the corresponding redistribution obligations, including LGPL obligations. This source repository does not vendor those servers or wheels.

The interface, inline SVG icons and synthetic dataset are generated for this project. The UI uses system fonts and no remote asset CDN. Installed browser binaries, generated videos, and reports are excluded from Git.
