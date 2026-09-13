from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

event_sink: ContextVar[Callable[[str, dict[str, Any]], None] | None] = ContextVar(
    "event_sink", default=None
)


def emit(event: str, data: dict[str, Any]) -> None:
    sink = event_sink.get()
    if sink:
        sink(event, data)
