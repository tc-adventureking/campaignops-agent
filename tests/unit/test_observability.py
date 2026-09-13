import json
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.workflow import Workflow
from app.domain.models import Metrics, RunRequest, RunStatus
from app.observability.reporting import price, report
from app.observability.store import RunStore


def test_pricing_cache_and_unknown(tmp_path) -> None:
    catalog = tmp_path / "prices.json"
    catalog.write_text(
        json.dumps(
            {
                "version": "unit-v1",
                "currency": "USD",
                "per_million_tokens": {"test": {"input": 2, "cached_input": 0.2, "output": 3}},
            }
        )
    )
    usage = price(
        Metrics(input_tokens=1000, cache_hit_tokens=800, output_tokens=100), "test", catalog
    )
    assert usage.estimated_cost == pytest.approx(0.00086)
    assert usage.price_version == "unit-v1"
    assert price(Metrics(input_tokens=1000), "unknown", catalog).estimated_cost is None
    assert price(Metrics(input_tokens=1000), "test", catalog).estimated_cost is None
    assert (
        price(
            Metrics(input_tokens=1000, cache_hit_tokens=0, usage_complete=False), "test", catalog
        ).estimated_cost
        is None
    )


async def test_trace_parents_report_redaction_and_retention(settings, tmp_path) -> None:
    store = RunStore(tmp_path / "state.db", ("sensitive-local-value",))
    run = await Workflow(settings.model_copy(update={"experiment_id": "exp_test"}), store).run(
        RunRequest(question="查询 CTR")
    )
    assert run.experiment_id == "exp_test"
    events = store.export_trace(run.trace_id)
    assert all(event.parent_span_id == run.root_span_id for event in events if event.span_id)
    result = report(store)
    assert result["sample_size"] == 1 and result["success_rate"] == 1
    assert result["stages"]["sql"]["p95_ms"] > 0
    old = store.create("demo", key="old-key-0001", request_hash="request")
    store.emit(old.run_id, "probe", {"password": "fake", "value": "sensitive-local-value"})
    store.transition(old.run_id, RunStatus.CANCELLED)
    assert "sensitive-local-value" not in store.path.read_bytes().decode(errors="ignore")
    with store._connect() as db:
        value = store.get(old.run_id).model_dump(mode="json")
        value["updated_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        db.execute("UPDATE runs SET payload=? WHERE run_id=?", [json.dumps(value), old.run_id])
    assert store.purge(30) == 1
    assert store.lookup_key("old-key-0001", "request") is None
    assert store.get(run.run_id).status == "succeeded"
