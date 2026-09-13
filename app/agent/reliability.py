"""Bounded retries and cooperative cancellation shared by async and thread tools."""

import asyncio
import random
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar

from app.agent.planning import TaskContext
from app.agent.runners import AgentRunner
from app.domain.models import AppError, ErrorCode, Evidence, Metrics, SQLPlan
from app.observability.reporting import price
from app.settings import Settings


class Cancellation:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.lock = threading.RLock()
        self.callbacks: list[Callable[[], None]] = []

    def check(self) -> None:
        if self.event.is_set():
            raise AppError(ErrorCode.CANCELLED, "任务已取消")

    def register(self, callback: Callable[[], None]) -> None:
        with self.lock:
            self.check()
            self.callbacks.append(callback)

    def unregister(self, callback: Callable[[], None]) -> None:
        with self.lock:
            if callback in self.callbacks:
                self.callbacks.remove(callback)

    def cancel(self) -> None:
        with self.lock:
            self.event.set()
            for callback in self.callbacks:
                try:
                    callback()
                except Exception:
                    # Server-side timeouts remain active if a connection is already gone.
                    continue


cancellation: ContextVar[Cancellation | None] = ContextVar("cancellation", default=None)


class ReliableRunner:
    def __init__(
        self,
        settings: Settings,
        factory: Callable[[Settings], AgentRunner],
        emit: Callable[[str, dict[str, object]], None],
    ):
        self.settings, self.factory, self.emit = settings, factory, emit
        self.metrics = Metrics()

    def accumulate(self, usage: Metrics) -> None:
        first = self.metrics.attempts == 1
        usage = price(usage, self.metrics.model_used or "unknown", self.settings.model_prices_path)
        self.metrics.price_version = usage.price_version
        self.metrics.cost_currency = usage.cost_currency
        self.metrics.estimated_cost = (
            usage.estimated_cost
            if first
            else (
                self.metrics.estimated_cost + usage.estimated_cost
                if self.metrics.estimated_cost is not None and usage.estimated_cost is not None
                else None
            )
        )
        self.metrics.input_tokens += usage.input_tokens
        self.metrics.output_tokens += usage.output_tokens
        for name in ("cache_hit_tokens", "cache_miss_tokens"):
            value, total = getattr(usage, name), getattr(self.metrics, name)
            setattr(
                self.metrics,
                name,
                value
                if first
                else (total + value if total is not None and value is not None else None),
            )

    async def plan(self, question: str, context: TaskContext, evidence: list[Evidence]) -> SQLPlan:
        names = list(dict.fromkeys([self.settings.model_name, *self.settings.model_fallbacks]))
        if self.settings.agent_mode == "demo":
            names = ["rule-demo"]
        for index, name in enumerate(names):
            self.metrics.model_used = name
            self.metrics.fallback_count = index
            if index:
                self.emit("fallback", {"model": name, "from_model": names[index - 1]})
            for attempt in range(self.settings.model_retries + 1):
                runner = self.factory(self.settings.model_copy(update={"model_name": name}))
                self.metrics.attempts += 1
                tick = time.perf_counter()
                error: AppError | None = None
                plan: SQLPlan | None = None
                try:
                    async with asyncio.timeout(self.settings.model_timeout_seconds):
                        plan = await runner.plan(question, context, evidence)
                except TimeoutError:
                    error = AppError(ErrorCode.MODEL_TIMEOUT, "模型总调用超时", True)
                except AppError as exc:
                    error = exc
                finally:
                    self.accumulate(runner.metrics)
                self.emit(
                    "model_attempt",
                    {
                        "model": name,
                        "attempt": attempt + 1,
                        "status": "error" if error else "ok",
                        "duration_ms": (time.perf_counter() - tick) * 1000,
                        "metrics": runner.metrics.model_dump(),
                        "error": error.info.model_dump(mode="json") if error else None,
                    },
                )
                if not error:
                    assert plan is not None
                    return plan
                # Failed requests may have been billed without returning usage.
                self.metrics.usage_complete = False
                self.metrics.estimated_cost = None
                if not error.info.retryable:
                    raise error
                if attempt < self.settings.model_retries:
                    delay = self.settings.model_retry_base_seconds * 2**attempt
                    delay *= random.uniform(0.5, 1.5)
                    self.metrics.retry_count += 1
                    self.emit(
                        "retry",
                        {"model": name, "delay_seconds": delay, "error_code": error.info.code},
                    )
                    await asyncio.sleep(delay)
                elif index == len(names) - 1:
                    raise error
        raise AssertionError("empty model chain")
