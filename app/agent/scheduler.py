"""Single-process admission control; durable idempotency remains in SQLite."""

import asyncio
import hashlib
import re

from app.agent.workflow import Workflow
from app.domain.models import TERMINAL, AppError, ErrorCode, RunRequest, RunResult, RunStatus
from app.observability.store import RunStore
from app.settings import Settings


class Scheduler:
    def __init__(self, settings: Settings, store: RunStore, workflow: Workflow):
        self.settings, self.store, self.workflow = settings, store, workflow
        self.slots = asyncio.Semaphore(settings.max_concurrent_runs)
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def submit(self, request: RunRequest, key: str | None) -> RunResult:
        if key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise AppError(ErrorCode.INPUT, "Idempotency-Key 须为 8–128 位字母、数字或 _ . : -")
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        if key:
            existing = self.store.lookup_key(key, fingerprint)
            if existing:
                return existing
        # No await between admission and registration: requests cannot oversubscribe.
        if len(self.tasks) >= self.settings.max_concurrent_runs + self.settings.max_queued_runs:
            raise AppError(ErrorCode.OVERLOADED, "执行和等待队列已满，请稍后重试", True)
        run = self.store.create(self.settings.agent_mode, key, fingerprint)
        task = asyncio.create_task(self._execute(request, run.run_id, key, fingerprint))
        self.tasks[run.run_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(run.run_id, None))
        return run

    async def _execute(
        self, request: RunRequest, run_id: str, key: str | None = None, fingerprint: str = ""
    ) -> None:
        try:
            if key:
                await asyncio.to_thread(
                    self.workflow.executor.cache.mirror,
                    "idempotency",
                    hashlib.sha256(key.encode()).hexdigest(),
                    {"run_id": run_id, "request_hash": fingerprint},
                )
            async with self.slots:
                await asyncio.to_thread(
                    self.workflow.executor.cache.mirror,
                    "run",
                    run_id,
                    {"run_id": run_id, "status": "running"},
                )
                await self.workflow.run(request, run_id)
        except asyncio.CancelledError:
            if self.store.get(run_id).status not in TERMINAL:
                self.store.transition(run_id, RunStatus.CANCELLED)
        except Exception:
            if self.store.get(run_id).status not in TERMINAL:
                self.store.transition(
                    run_id,
                    RunStatus.FAILED,
                    error={
                        "code": ErrorCode.SYSTEM,
                        "message": "执行器异常，请查询 Trace",
                        "retryable": True,
                    },
                )
        finally:
            await asyncio.to_thread(
                self.workflow.executor.cache.mirror,
                "run",
                run_id,
                self.store.get(run_id).model_dump(mode="json"),
            )

    async def cancel(self, run_id: str) -> RunResult:
        run = self.store.get(run_id)
        if run.status in TERMINAL:
            return run
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.store.get(run_id).status not in TERMINAL:
            self.store.transition(run_id, RunStatus.CANCELLED)
        return self.store.get(run_id)

    async def close(self) -> None:
        await asyncio.gather(*(self.cancel(run_id) for run_id in list(self.tasks)))
