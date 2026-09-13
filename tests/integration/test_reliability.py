import asyncio
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.planning import task_context
from app.agent.reliability import ReliableRunner, cancellation
from app.agent.runners import DemoRunner, OpenAICompatibleRunner
from app.agent.workflow import Workflow
from app.api.main import create_app
from app.domain.models import AppError, Metrics, RunRequest
from app.observability.store import RunStore
from app.settings import Settings


def test_idempotency_queue_cancel_restart(settings: Settings) -> None:
    config = settings.model_copy(update={"max_concurrent_runs": 1, "max_queued_runs": 1})
    api = create_app(config)
    body = {"question": "查询 CTR"}
    headers = {"Idempotency-Key": "repeat-0001"}
    with TestClient(api) as client:

        async def slow(*args, **kwargs):
            await asyncio.sleep(30)

        api.state.workflow.run = slow
        first = client.post("/v1/runs", json=body, headers=headers).json()
        second = client.post("/v1/runs", json=body).json()
        assert client.post("/v1/runs", json=body).status_code == 429
        assert (
            client.post("/v1/runs", json=body, headers=headers).json()["run_id"] == first["run_id"]
        )
        assert (
            client.post("/v1/runs", json={"question": "查询 CVR"}, headers=headers).status_code
            == 409
        )
        assert client.post(f"/v1/runs/{second['run_id']}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/v1/runs/{first['run_id']}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/v1/runs/{first['run_id']}/cancel").status_code == 200
    with TestClient(create_app(config)) as client:
        replay = client.post("/v1/runs", json=body, headers=headers).json()
        assert replay["run_id"] == first["run_id"] and replay["status"] == "cancelled"


@pytest.mark.parametrize(
    "status,retryable", [(429, True), (503, True), (401, False), (400, False), (200, False)]
)
async def test_retry_only_transient(settings: Settings, status: int, retryable: bool) -> None:
    config = settings.model_copy(
        update={
            "agent_mode": "openai",
            "model_retry_base_seconds": 0,
            "model_api_key": Settings(_env_file=None, model_api_key="fake").model_api_key,
        }
    )
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json={"invalid": True})

    runner = ReliableRunner(
        config,
        lambda cfg: OpenAICompatibleRunner(cfg, httpx.MockTransport(respond)),
        lambda *_: None,
    )
    request = RunRequest(question="查询 CTR")
    with pytest.raises(AppError):
        await runner.plan(request.question, task_context(request, settings.data_dir), [])
    assert count == (3 if retryable else 1)


async def test_fallback_and_usage(settings: Settings) -> None:
    events = []
    config = settings.model_copy(
        update={
            "agent_mode": "openai",
            "model_name": "primary",
            "model_fallbacks": ["backup"],
            "model_retries": 1,
            "model_retry_base_seconds": 0,
            "model_timeout_seconds": 0.01,
        }
    )

    class Runner(DemoRunner):
        def __init__(self, cfg):
            super().__init__()
            self.cfg = cfg

        async def plan(self, *args):
            if self.cfg.model_name == "primary":
                await asyncio.sleep(1)
            self.metrics = Metrics(input_tokens=10, output_tokens=5)
            return await super().plan(*args)

    runner = ReliableRunner(config, Runner, lambda e, d: events.append((e, d)))
    request = RunRequest(question="查询 CTR")
    assert await runner.plan(request.question, task_context(request, settings.data_dir), [])
    assert runner.metrics.model_used == "backup"
    assert runner.metrics.retry_count == 1 and runner.metrics.fallback_count == 1
    assert runner.metrics.input_tokens == 10 and not runner.metrics.usage_complete
    assert any(e == "fallback" for e, _ in events)


async def test_connection_recovery(settings: Settings) -> None:
    attempts = 0

    class Flaky(DemoRunner):
        async def plan(self, *args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise AppError("model_unavailable", "connection failed", True)
            return await super().plan(*args)

    runner = ReliableRunner(
        settings.model_copy(update={"model_retry_base_seconds": 0}),
        lambda _: Flaky(),
        lambda *_: None,
    )
    request = RunRequest(question="查询 CTR")
    assert await runner.plan(request.question, task_context(request, settings.data_dir), [])
    assert attempts == 2


@pytest.mark.parametrize("deadline", [False, True])
async def test_cancel_reaches_thread(settings: Settings, tmp_path: Path, deadline: bool) -> None:
    config = settings.model_copy(update={"run_timeout_seconds": 0.1 if deadline else 30})
    store = RunStore(tmp_path / "state.db")
    workflow = Workflow(config, store)
    entered, stopped = threading.Event(), threading.Event()

    def slow(*args):
        scope = cancellation.get()
        assert scope is not None
        entered.set()
        scope.event.wait(2)
        stopped.set()
        scope.check()

    workflow.retriever.retrieve = slow
    run = store.create("demo")
    task = asyncio.create_task(workflow.run(RunRequest(question="查询 CTR"), run.run_id))
    await asyncio.to_thread(entered.wait, 1)
    if not deadline:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert stopped.is_set()
    final = store.get(run.run_id)
    assert final.status == ("failed" if deadline else "cancelled")
    if deadline:
        assert final.error.code == "run_timeout"
    assert store.events(run.run_id)[-1].data["status"] == final.status
