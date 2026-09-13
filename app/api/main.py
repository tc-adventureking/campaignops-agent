import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr

from app.agent.scheduler import Scheduler
from app.agent.workflow import Workflow
from app.api.presentation import MarkdownRequest, MarkdownResponse, compile_markdown
from app.domain.models import TERMINAL, AppError, ErrorCode, RunRequest, RunResult
from app.observability.reporting import report
from app.observability.store import RunStore
from app.settings import Settings
from app.tools.approvals import BudgetChange, Decision, Execution

HTTP_CODES = {
    ErrorCode.INPUT: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.SQL_REJECTED: 422,
    ErrorCode.OVERLOADED: 429,
    ErrorCode.MODEL_UNAVAILABLE: 502,
    ErrorCode.MODEL_INVALID: 502,
    ErrorCode.MODEL_TOOL_UNSUPPORTED: 502,
    ErrorCode.MODEL_TIMEOUT: 504,
    ErrorCode.QUERY_TIMEOUT: 504,
    ErrorCode.RUN_TIMEOUT: 504,
    ErrorCode.FORBIDDEN: 403,
}


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        api.state.store = RunStore(
            config.state_path,
            tuple(
                value.get_secret_value()
                for value in config.__dict__.values()
                if isinstance(value, SecretStr)
            ),
        )
        api.state.store.recover_interrupted()
        api.state.store.purge(config.trace_retention_days)
        api.state.workflow = Workflow(config, api.state.store)
        api.state.scheduler = Scheduler(config, api.state.store, api.state.workflow)
        yield
        await api.state.scheduler.close()
        api.state.workflow.executor.cache.close()

    api = FastAPI(
        title="CampaignOps Agent",
        version="0.2.0rc1",
        lifespan=lifespan,
        description="合成广告数据只读诊断。demo 为离线规则模式；真实模型请配置 .env。单进程本地 MVP。",
    )

    @api.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_CODES.get(exc.info.code, 500),
            content={"error": exc.info.model_dump(mode="json")},
        )

    @api.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_input",
                    "message": "请求字段格式、长度或范围无效",
                    "fields": [list(item["loc"]) for item in exc.errors()],
                }
            },
        )

    @api.exception_handler(Exception)
    async def system_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"error": {"code": "system_error", "message": "服务内部错误"}}
        )

    static_dir = Path(__file__).parent / "static"
    api.mount("/static", StaticFiles(directory=static_dir), name="static")

    @api.get("/", include_in_schema=False)
    async def home() -> FileResponse:
        return FileResponse(
            static_dir / "index.html",
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @api.post("/v1/markdown", response_model=MarkdownResponse)
    def markdown(body: MarkdownRequest) -> MarkdownResponse:
        return MarkdownResponse(html=compile_markdown(body.markdown))

    @api.get("/observability", include_in_schema=False)
    def observability_page() -> FileResponse:
        return FileResponse(static_dir / "observability.html")

    @api.get("/v1/observability")
    def observability(limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
        return report(api.state.store, limit)

    @api.get("/v1/traces/{trace_id}")
    def trace(trace_id: str) -> list[dict[str, Any]]:
        return [event.model_dump(mode="json") for event in api.state.store.export_trace(trace_id)]

    @api.post("/v1/approvals", status_code=201)
    def propose(
        body: BudgetChange,
        x_operator_key: str | None = Header(default=None),
        run_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        api.state.workflow.approvals.authenticate(x_operator_key)
        if run_id and api.state.store.get(run_id).status != "approval_required":
            raise AppError(ErrorCode.CONFLICT, "仅待审批任务可关联模拟提议")
        return dict(api.state.workflow.approvals.propose(body, run_id))

    @api.get("/v1/approvals/{identifier}")
    def approval(
        identifier: str, x_operator_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        api.state.workflow.approvals.authenticate(x_operator_key)
        return dict(api.state.workflow.approvals.get(identifier))

    @api.post("/v1/approvals/{identifier}/decision")
    def decision(
        identifier: str, body: Decision, x_operator_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        service = api.state.workflow.approvals
        return dict(service.decide(identifier, body, service.authenticate(x_operator_key)))

    @api.post("/v1/approvals/{identifier}/execute")
    def execute_approval(
        identifier: str, body: Execution, x_operator_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        service = api.state.workflow.approvals
        return dict(service.execute(identifier, body, service.authenticate(x_operator_key)))

    @api.post("/v1/approvals/{identifier}/rollback")
    def rollback(
        identifier: str, x_operator_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        service = api.state.workflow.approvals
        service.authenticate(x_operator_key)
        return dict(service.rollback_proposal(identifier))

    @api.get("/v1/approvals/{identifier}/audit")
    def approval_audit(
        identifier: str, x_operator_key: str | None = Header(default=None)
    ) -> list[dict[str, Any]]:
        service = api.state.workflow.approvals
        service.authenticate(x_operator_key)
        return list(service.audit(identifier))

    @api.get("/v1/workspace")
    def workspace() -> dict[str, Any]:
        try:
            manifest = json.loads((config.data_dir / "manifest.json").read_text())
        except (OSError, ValueError):
            raise AppError(ErrorCode.NOT_FOUND, "数据尚未初始化") from None
        return {
            "mode": config.agent_mode,
            "data": {
                key: manifest.get(key)
                for key in (
                    "version",
                    "days",
                    "rows",
                    "start_date",
                    "end_date",
                    "currency",
                    "timezone",
                )
            },
            "knowledge": [
                {
                    "chunk_id": chunk.chunk_id,
                    "title": chunk.summary.split("\n", 1)[0].removeprefix("## ").split(" {#", 1)[0],
                    "version": chunk.version,
                }
                for chunk in api.state.workflow.retriever.chunks
            ],
        }

    @api.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @api.get("/health/ready")
    async def ready() -> JSONResponse:
        checks = {
            "database": config.database_path.is_file()
            if config.database_backend == "duckdb"
            else bool(config.postgres_dsn.get_secret_value()),
            "manifest": (config.data_dir / "manifest.json").is_file(),
            "knowledge": bool(api.state.workflow.retriever.chunks),
            "model_config": config.agent_mode == "demo"
            or bool(config.model_api_key.get_secret_value()),
        }
        if checks["database"]:
            try:
                await asyncio.to_thread(
                    api.state.workflow.executor.execute, "SELECT COUNT(*) AS n FROM daily_metrics"
                )
            except AppError:
                checks["database"] = False
        return JSONResponse(
            status_code=200 if all(checks.values()) else 503,
            content={
                "status": "ready" if all(checks.values()) else "not_ready",
                "mode": config.agent_mode,
                "checks": checks,
                "model_network_checked": False,
            },
        )

    @api.post(
        "/v1/runs",
        response_model=RunResult,
        status_code=202,
        responses={422: {"description": "请求格式错误"}, 429: {"description": "并发任务上限"}},
    )
    async def create_run(
        body: RunRequest, idempotency_key: str | None = Header(default=None)
    ) -> RunResult:
        return RunResult.model_validate(api.state.scheduler.submit(body, idempotency_key))

    @api.post("/v1/runs/{run_id}/cancel", response_model=RunResult)
    async def cancel_run(run_id: str) -> RunResult:
        return RunResult.model_validate(await api.state.scheduler.cancel(run_id))

    @api.get("/v1/runs/{run_id}", response_model=RunResult)
    async def get_run(run_id: str) -> RunResult:
        return RunResult.model_validate(api.state.store.get(run_id))

    @api.get("/v1/runs/{run_id}/events")
    async def events(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        store: RunStore = api.state.store
        store.get(run_id)
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
            if cursor < 0:
                raise ValueError
        except ValueError:
            raise AppError(ErrorCode.INPUT, "Last-Event-ID 必须是非负整数") from None
        existing = store.events(run_id)
        if cursor > (existing[-1].event_id if existing else 0):
            raise AppError(ErrorCode.CONFLICT, "Last-Event-ID 超出已保存事件范围")

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            heartbeat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                batch = store.events(run_id, cursor)
                for item in batch:
                    cursor = item.event_id
                    yield f"id: {item.event_id}\nevent: {item.event}\ndata: {item.model_dump_json()}\n\n"
                if store.get(run_id).status in TERMINAL:
                    # A terminal transition may have committed after the preceding read.
                    for item in store.events(run_id, cursor):
                        cursor = item.event_id
                        yield f"id: {item.event_id}\nevent: {item.event}\ndata: {item.model_dump_json()}\n\n"
                    break
                if time.monotonic() - heartbeat >= 5:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.05)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.get("/v1/knowledge/{chunk_id}")
    async def knowledge(chunk_id: str) -> dict[str, Any]:
        for chunk in api.state.workflow.retriever.chunks:
            if chunk.chunk_id == chunk_id:
                return dict(chunk.model_dump(mode="json"))
        raise AppError(ErrorCode.NOT_FOUND, "知识片段不存在")

    return api


app = create_app()
