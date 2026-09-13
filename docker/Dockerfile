FROM ghcr.io/astral-sh/uv:0.12.10 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra youtu --extra storage
COPY app ./app
COPY configs ./configs
COPY data ./data
COPY scripts ./scripts
COPY docker/entrypoint.sh ./docker/entrypoint.sh
COPY docker/init_postgres.sh ./docker/init_postgres.sh
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
ENTRYPOINT ["sh", "docker/entrypoint.sh"]
