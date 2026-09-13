import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from app.agent.planning import diagnostic_sql, task_context
from app.agent.reliability import Cancellation, ReliableRunner, cancellation
from app.agent.report import diagnostic_facts, render
from app.agent.runners import AgentRunner, build_runner
from app.domain.models import (
    Answer,
    AppError,
    ApprovalRequest,
    ErrorCode,
    Metrics,
    QueryData,
    RunRequest,
    RunResult,
    RunStatus,
    ToolCall,
)
from app.guardrails.injection import require_data
from app.guardrails.sql import validate_sql
from app.observability.context import event_sink
from app.observability.store import RunStore
from app.settings import Settings
from app.tools.analysis import analyze
from app.tools.approvals import ApprovalService
from app.tools.retrieval import LocalRetriever, validate_citations
from app.tools.sql import SQLExecutor

T = TypeVar("T", bound=BaseModel)


class Workflow:
    def __init__(
        self,
        settings: Settings,
        store: RunStore,
        runner_factory: Callable[[], AgentRunner] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.retriever = LocalRetriever(settings.knowledge_dir)
        self.executor = SQLExecutor(settings)
        self.approvals = ApprovalService(settings, store)
        self.runner_factory = runner_factory

    async def run(self, request: RunRequest, run_id: str | None = None) -> RunResult:
        run = (
            self.store.get(run_id)
            if run_id
            else self.store.create(
                self.settings.agent_mode, experiment_id=self.settings.experiment_id
            )
        )
        scope = Cancellation()
        token = cancellation.set(scope)
        sink_token = event_sink.set(lambda event, data: self.store.emit(run.run_id, event, data))
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                return await self._execute(request, run)
        except TimeoutError:
            scope.cancel()
            return self.store.transition(
                run.run_id,
                RunStatus.FAILED,
                error={
                    "code": ErrorCode.RUN_TIMEOUT,
                    "message": "任务超过总执行时限",
                    "retryable": True,
                },
                metrics=run.metrics.model_copy(
                    update={
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "usage_complete": False,
                        "estimated_cost": None,
                    }
                ).model_dump(),
            )
        except asyncio.CancelledError:
            scope.cancel()
            self.store.transition(
                run.run_id,
                RunStatus.CANCELLED,
                metrics=run.metrics.model_copy(
                    update={
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "usage_complete": False,
                        "estimated_cost": None,
                    }
                ).model_dump(),
            )
            raise
        finally:
            cancellation.reset(token)
            event_sink.reset(sink_token)

    async def _execute(self, request: RunRequest, run: RunResult) -> RunResult:
        started = time.perf_counter()
        calls = 0
        runner: AgentRunner | None = None
        active_span: str | None = None
        query_hashes: set[str] = set()
        query_data: QueryData | None = None
        self.store.transition(run.run_id, RunStatus.RUNNING)

        async def tool(
            name: str, operation: Callable[[], T], arguments: dict[str, Any] | None = None
        ) -> T:
            nonlocal calls, active_span
            calls += 1
            if calls > self.settings.max_tool_calls:
                raise AppError(ErrorCode.LIMIT, "达到最大工具调用次数")
            active_span = "span_" + uuid4().hex
            tick = time.perf_counter()
            call = ToolCall(
                span_id=active_span, name=name, status="started", arguments=arguments or {}
            )
            self.store.emit(run.run_id, "tool", call.model_dump(mode="json"), active_span)
            if name in {"sql", "diagnostic_sql"}:
                sql = str((arguments or {}).get("sql", ""))
                try:
                    checked = validate_sql(sql, self.settings.max_query_rows)
                except AppError as error:
                    self.store.emit(
                        run.run_id,
                        "sql_validation",
                        {"decision": "rejected", "error": error.info.model_dump(mode="json")},
                        active_span,
                    )
                    raise
                if checked.query_hash in query_hashes:
                    raise AppError(ErrorCode.LIMIT, "重复 SQL 无进展，运行已中止")
                query_hashes.add(checked.query_hash)
                self.store.emit(
                    run.run_id,
                    "sql_validation",
                    {
                        "decision": "allowed",
                        "normalized_sql": checked.normalized,
                        "query_hash": checked.query_hash,
                    },
                    active_span,
                )
            pending = asyncio.create_task(asyncio.to_thread(operation))
            try:
                timeout = (
                    self.settings.retrieval_timeout_seconds
                    if name == "retrieve"
                    else self.settings.query_timeout_seconds
                    + self.settings.database_connect_timeout_seconds
                    + 1
                )
                async with asyncio.timeout(timeout):
                    result = await asyncio.shield(pending)
            except (asyncio.CancelledError, TimeoutError) as exc:
                scope = cancellation.get()
                if scope:
                    scope.cancel()
                # SQL interruption must finish before the scheduler frees its slot.
                await asyncio.gather(pending, return_exceptions=True)
                if isinstance(exc, TimeoutError):
                    raise AppError(
                        ErrorCode.RETRIEVAL if name == "retrieve" else ErrorCode.QUERY_TIMEOUT,
                        "工具执行超时",
                        True,
                    ) from None
                raise
            call.status, call.result = "ok", result.model_dump(mode="json")
            if name in {"retrieve", "sql", "diagnostic_sql"}:
                # Only inspect payload data. SQL plans themselves are already AST validated.
                require_data(
                    json.dumps(
                        getattr(result, "data", None),
                        default=lambda obj: obj.model_dump(),
                        ensure_ascii=False,
                    ),
                    name,
                )
            self.store.emit(
                run.run_id,
                "tool",
                call.model_dump(mode="json") | {"duration_ms": (time.perf_counter() - tick) * 1000},
                active_span,
            )
            return result

        try:
            context = task_context(request, self.settings.data_dir)
            self.store.emit(
                run.run_id,
                "context",
                {
                    "intent": context.intent,
                    "start_date": str(context.start),
                    "end_date": str(context.end),
                    "campaign_id": context.campaign_id,
                    "knowledge_version": self.retriever.version,
                },
            )
            if context.intent == "approval":
                proposal = ApprovalRequest(
                    summary="请求涉及预算、出价或投放状态变更，等待人工确认；真实投放不会变更。"
                )
                self.store.emit(run.run_id, "approval", proposal.model_dump(mode="json"))
                return self.store.transition(
                    run.run_id,
                    RunStatus.APPROVAL_REQUIRED,
                    approval=proposal.model_dump(mode="json"),
                )
            if context.intent == "refusal":
                answer = Answer(
                    conclusion="已拒绝：请求涉及越权、泄密或绕过安全策略。",
                    evidence=[],
                    limitations=["仅支持合成广告数据的只读分析。"],
                )
            elif context.intent == "insufficient":
                answer = Answer(
                    conclusion="信息不足：只有 90 天合成数据，无法完成去年同期比较。",
                    evidence=[],
                    limitations=["需要完整且口径一致的去年同期数据。"],
                )
            else:
                retrieval = await tool(
                    "retrieve",
                    lambda: self.retriever.retrieve(request.question, 5),
                    {"query_chars": len(request.question)},
                )
                citations = retrieval.data or []
                if not citations:
                    answer = Answer(
                        conclusion="未检索到足够相关的业务口径，请补充具体指标或广告问题。",
                        evidence=[],
                        limitations=["不会在缺少口径时生成 SQL 或给出确定性结论。"],
                    )
                elif context.intent == "definition":
                    answer = Answer(
                        conclusion="以下为本项目知识库的指标与业务口径。",
                        evidence=citations,
                        citations=citations,
                        confidence=1,
                        facts=[
                            f"{c.summary.split(chr(10), 1)[-1]} [{c.chunk_id}]" for c in citations
                        ],
                        limitations=["仅适用于本项目合成数据及引用文档的版本。"],
                    )
                else:
                    active_span = "span_" + uuid4().hex
                    runner = (
                        self.runner_factory()
                        if self.runner_factory
                        else ReliableRunner(
                            self.settings,
                            build_runner,
                            lambda event, data: self.store.emit(
                                run.run_id, event, data, active_span
                            ),
                        )
                    )
                    self.store.emit(
                        run.run_id,
                        "model",
                        {
                            "status": "started",
                            "mode": self.settings.agent_mode,
                            "model": "rule-demo"
                            if self.settings.agent_mode == "demo"
                            else self.settings.model_name,
                        },
                        active_span,
                    )
                    plan = await runner.plan(request.question, context, citations)
                    if plan.start_date != context.start or plan.end_date != context.end:
                        raise AppError(ErrorCode.SQL_GENERATION, "模型计划的日期范围与请求不一致")
                    self.store.emit(
                        run.run_id,
                        "model",
                        {
                            "status": "ok",
                            "plan": plan.model_dump(mode="json"),
                            "metrics": runner.metrics.model_dump(),
                        },
                        active_span,
                    )
                    queried = await tool(
                        "sql", lambda: self.executor.execute(plan.sql), {"sql": plan.sql}
                    )
                    assert queried.data is not None
                    query_data = queried.data
                    self.store.emit(
                        run.run_id,
                        "evidence",
                        {"evidence": [e.model_dump() for e in queried.evidence]},
                    )
                    answer = Answer(
                        conclusion=f"已完成只读查询，返回 {queried.data.row_count} 行。",
                        evidence=queried.evidence,
                        citations=citations,
                        confidence=1 if queried.data.row_count else 0,
                        facts=[
                            json.dumps(row, ensure_ascii=False)
                            + f" [SQL {queried.data.query_hash}]"
                            for row in queried.data.rows[:20]
                        ],
                        limitations=context.assumptions + plan.assumptions,
                    )
                    if not queried.data.row_count or not any(
                        value is not None for row in queried.data.rows for value in row.values()
                    ):
                        answer.conclusion = "查询范围内没有数据，无法给出确定性结论。"
                        answer.confidence = 0
                    if queried.data.truncated:
                        answer.limitations.append(
                            "结果达到行数上限，可能被截断；请缩小范围后再解读。"
                        )
                    if context.intent == "diagnosis":
                        canonical = diagnostic_sql(context)
                        canonical_hash = validate_sql(
                            canonical, self.settings.max_query_rows
                        ).query_hash
                        diagnostic = (
                            queried
                            if canonical_hash == queried.data.query_hash
                            else await tool(
                                "diagnostic_sql",
                                lambda: self.executor.execute(canonical),
                                {"sql": canonical},
                            )
                        )
                        assert diagnostic.data is not None
                        if diagnostic.data.truncated:
                            raise AppError(ErrorCode.ANALYSIS, "诊断数据可能被截断，请缩小范围")
                        diagnostic_rows = diagnostic.data.rows
                        result = await tool(
                            "analyze", lambda: analyze(diagnostic_rows, context.days)
                        )
                        assert result.data is not None
                        analysis = result.data
                        if diagnostic is not queried:
                            answer.evidence += diagnostic.evidence
                        answer.analysis = analysis
                        answer.root_causes = analysis.root_causes
                        for cause in answer.root_causes:
                            cause.supporting_evidence = [
                                f"{text} [SQL {diagnostic.data.query_hash}]"
                                for text in cause.supporting_evidence
                            ]
                        answer.confidence = max(
                            (c.confidence for c in analysis.root_causes), default=0
                        )
                        answer.facts = diagnostic_facts(analysis, diagnostic.data.query_hash)
                        answer.inferences = [
                            f"候选 {index + 1}：{cause.title}；贡献={cause.contribution:.1%}；置信度={cause.confidence:.0%}"
                            for index, cause in enumerate(analysis.root_causes)
                        ]
                        answer.limitations += analysis.limitations
                        answer.conclusion = (
                            "数据不足，无法可靠诊断。"
                            if not analysis.sufficient_data
                            else (
                                "首要候选原因：" + analysis.root_causes[0].title
                                if analysis.root_causes
                                else "未发现达到规则阈值的显著异常。"
                            )
                        )
                        answer.recommendations = (
                            [
                                "优先核查异常维度的创意、落地页和竞价变更，结合对照实验确认原因。",
                                "预算建议须由人工审批；MVP 不执行任何投放变更。",
                            ]
                            if analysis.root_causes
                            else []
                        )
                validate_citations(answer.citations, citations)
            answer.limitations = list(dict.fromkeys(answer.limitations))
            if self.settings.agent_mode == "demo":
                answer.limitations.append("离线规则演示，不能用本结果评估真实模型能力。")
            answer.markdown = render(answer, self.settings.agent_mode, query_data)
            metrics = (
                runner.metrics
                if runner
                else Metrics(estimated_cost=0, price_version="no-model-call", cost_currency="USD")
            )
            if metrics.fallback_count:
                answer.limitations.append(f"主模型不可用，本次已降级至 {metrics.model_used}。")
                answer.markdown = render(answer, self.settings.agent_mode, query_data)
            metrics.duration_ms = (time.perf_counter() - started) * 1000
            return self.store.transition(
                run.run_id,
                RunStatus.SUCCEEDED,
                answer=answer.model_dump(mode="json"),
                metrics=metrics.model_dump(),
            )
        except asyncio.CancelledError:
            run.metrics = runner.metrics if runner else Metrics()
            raise
        except Exception as exc:
            info = (
                exc.info
                if isinstance(exc, AppError)
                else AppError(ErrorCode.SYSTEM, "任务执行失败，请按 trace_id 检查失败阶段").info
            )
            self.store.emit(
                run.run_id,
                "tool_error",
                {"error": info.model_dump(mode="json"), "status": "error"},
                active_span,
            )
            return self.store.transition(
                run.run_id,
                RunStatus.FAILED,
                error=info.model_dump(mode="json"),
                metrics=(runner.metrics if runner else Metrics())
                .model_copy(update={"duration_ms": (time.perf_counter() - started) * 1000})
                .model_dump(),
            )
