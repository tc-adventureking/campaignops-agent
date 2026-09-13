import asyncio
from pathlib import Path

import pytest

from app.agent.runners import DemoRunner
from app.agent.workflow import Workflow
from app.domain.models import RunRequest, RunStatus
from app.observability.store import RunStore
from app.settings import Settings


@pytest.mark.parametrize(
    ("cid", "cause"),
    [
        (1, "traffic_drop"),
        (2, "ctr_drop"),
        (3, "cvr_drop"),
        (4, "cpc_rise"),
        (5, "budget_limited"),
        (6, "channel_anomaly"),
    ],
)
async def test_six_anomalies(settings: Settings, tmp_path: Path, cid: int, cause: str) -> None:
    store = RunStore(tmp_path / "runs.db")
    workflow = Workflow(settings, store)
    run = await workflow.run(RunRequest(question=f"诊断 Campaign {cid} 最近7天异常的原因"))
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert run.answer and run.answer.root_causes[0].code == cause
    assert run.answer.evidence[0].query_hash
    assert run.answer.citations
    tools = [
        event.data["name"]
        for event in store.events(run.run_id)
        if event.event == "tool" and event.data["status"] == "started"
    ]
    assert tools == ["retrieve", "sql", "analyze"]


async def test_normal_control(settings: Settings, tmp_path: Path) -> None:
    workflow = Workflow(settings, RunStore(tmp_path / "runs.db"))
    run = await workflow.run(
        RunRequest(question="诊断 Campaign 3", start_date="2026-08-09", end_date="2026-08-15")
    )
    assert run.answer and not run.answer.root_causes
    assert run.answer.analysis.sufficient_data


async def test_empty_and_approval(settings: Settings, tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    workflow = Workflow(settings, store)
    empty = await workflow.run(
        RunRequest(question="诊断数据异常", start_date="2027-01-01", end_date="2027-01-07")
    )
    assert empty.answer and not empty.answer.root_causes and empty.answer.confidence == 0
    assert empty.answer.analysis and not empty.answer.analysis.sufficient_data
    assert empty.answer.analysis.current["impressions"] is None
    assert empty.answer.analysis.previous["spend"] is None
    assert all("本期=无数据；上期=无数据" in fact for fact in empty.answer.facts)
    assert "| 曝光量 | 无数据 | 无数据 | — |" in empty.answer.markdown
    approval = await workflow.run(RunRequest(question="把 Campaign 3 的预算提高 20%"))
    assert approval.status == RunStatus.APPROVAL_REQUIRED and not approval.approval.executable
    assert not any(e.event == "tool" for e in store.events(approval.run_id))


async def test_failure_span(settings: Settings, tmp_path: Path) -> None:
    class UnsafeRunner(DemoRunner):
        async def plan(self, question, context, evidence):
            plan = await super().plan(question, context, evidence)
            plan.sql = "DROP TABLE daily_metrics"
            return plan

    store = RunStore(tmp_path / "runs.db")
    workflow = Workflow(settings, store, UnsafeRunner)
    run = await workflow.run(RunRequest(question="查询 CTR"))
    assert run.status == RunStatus.FAILED and run.error.code == "sql_rejected"
    error = next(event for event in store.events(run.run_id) if event.event == "tool_error")
    assert error.span_id


async def test_concurrent_runs_isolated(settings: Settings, tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    workflow = Workflow(settings, store)
    results = await asyncio.gather(
        *[workflow.run(RunRequest(question=f"诊断 Campaign {cid}")) for cid in (1, 2, 3)]
    )
    assert len({run.trace_id for run in results}) == 3
    for run in results:
        assert run.status == RunStatus.SUCCEEDED
        assert all(event.trace_id == run.trace_id for event in store.events(run.run_id))


async def test_definition_answers_zero_clicks_directly(settings: Settings, tmp_path: Path) -> None:
    workflow = Workflow(settings, RunStore(tmp_path / "runs.db"))
    run = await workflow.run(RunRequest(question="CTR 在点击数为零时如何计算"))
    assert run.status == RunStatus.SUCCEEDED and run.answer
    assert "点击为零且曝光大于零时，CTR 为 0" in run.answer.markdown
    assert "曝光为零时，CTR 为 null" in run.answer.markdown
    assert all(
        any(f"[{citation.chunk_id}]" in fact for fact in run.answer.facts)
        for citation in run.answer.citations
    )
