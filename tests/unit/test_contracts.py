from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.models import AppError, RunRequest, RunStatus
from app.observability.store import RunStore, redact


@pytest.mark.parametrize(
    "fields",
    [
        {"question": "  "},
        {"question": "x"},
        {"question": "ok", "start_date": "2026-08-29"},
        {"question": "ok", "start_date": "2026-08-29", "end_date": "2026-08-01"},
        {"question": "ok", "campaign_id": 7},
        {"question": "x" * 2001},
    ],
)
def test_request_validation(fields: dict) -> None:
    with pytest.raises(ValidationError):
        RunRequest.model_validate(fields)


def test_terminal_state_and_recovery(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    first = store.create("demo")
    store.transition(first.run_id, RunStatus.RUNNING)
    store.transition(first.run_id, RunStatus.SUCCEEDED)
    with pytest.raises(AppError):
        store.transition(first.run_id, RunStatus.FAILED)
    second = store.create("demo")
    store.recover_interrupted()
    assert store.get(first.run_id).status == RunStatus.SUCCEEDED
    assert store.get(second.run_id).status == RunStatus.FAILED
    events = store.export_trace(first.trace_id)
    assert [event.event_id for event in events] == list(range(1, len(events) + 1))


def test_redaction_before_storage(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db", ("secret-value-123",))
    run = store.create("demo")
    store.emit(
        run.run_id,
        "test",
        {
            "api_key": "top-secret",
            "text": "secret-value-123 sk-abcdefgh hello@example.com Bearer qwerty",
        },
    )
    serialized = str(store.events(run.run_id))
    assert all(
        secret not in serialized
        for secret in [
            "secret-value-123",
            "sk-abcdefgh",
            "hello@example.com",
            "qwerty",
            "top-secret",
        ]
    )
    assert redact({"password": "pass"})["password"] == "[REDACTED]"
