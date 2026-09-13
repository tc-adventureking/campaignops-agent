import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.agent.planning import TaskContext, model_context
from app.agent.prompts import prompt_hash, system_prompt
from app.observability.model_usage import CacheUsageCollector, cache_counts


def test_request_context_keeps_variable_data_out_of_system_prefix() -> None:
    question = "查询独立测试 Campaign 的消耗"
    evidence = [{"chunk_id": "unique-test-chunk", "text": "独立检索片段"}]
    context = TaskContext("query", date(2026, 1, 8), date(2026, 1, 14), date(2026, 1, 1), 3, [])
    prefix = system_prompt()
    payload = json.loads(model_context(question, context, evidence))
    assert payload["question_untrusted"] == question
    assert payload["retrieved_untrusted"] == evidence
    assert payload["previous_start_date"] == "2026-01-01"
    assert payload["previous_end_date"] == "2026-01-07"
    assert "schema" not in payload and "metrics" not in payload
    assert '"conversion_value"' in prefix and '"cvr"' in prefix
    assert question not in prefix and "unique-test-chunk" not in prefix
    assert "2026-01-08" not in prefix


def test_prompt_hash_covers_appended_static_config(tmp_path: Path, monkeypatch) -> None:
    original_hash = prompt_hash()
    directory = tmp_path / "configs"
    directory.mkdir()
    for path in Path("configs").iterdir():
        if path.is_file():
            (directory / path.name).write_bytes(path.read_bytes())
    monkeypatch.chdir(tmp_path)
    assert prompt_hash() == original_hash
    schema_path = directory / "schema.json"
    schema = json.loads(schema_path.read_text())
    schema["tables"]["daily_metrics"]["spend"]["description"] += "（测试修订）"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    assert prompt_hash() != original_hash


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
            (80, 20),
        ),
        ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 70}}, (70, 30)),
        ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 0}, (0, 100)),
        ({"prompt_tokens": 100}, (None, None)),
        ({"prompt_cache_hit_tokens": -1, "prompt_cache_miss_tokens": True}, (None, None)),
        ({"prompt_tokens": 10, "prompt_cache_hit_tokens": 20}, (20, None)),
    ],
)
def test_provider_cache_counts(usage, expected) -> None:
    actual = cache_counts(usage)
    assert (actual["cache_hit_tokens"], actual["cache_miss_tokens"]) == expected


async def test_collector_preserves_unknown_usage_and_skips_streams() -> None:
    collector = CacheUsageCollector()
    assert collector.totals() == {"cache_hit_tokens": None, "cache_miss_tokens": None}
    for hit in (70, 80):
        await collector.on_response(
            httpx.Response(
                200, json={"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": hit}}
            )
        )
    assert collector.totals() == {"cache_hit_tokens": 150, "cache_miss_tokens": 50}
    await collector.on_response(httpx.Response(503, json={"error": "unavailable"}))
    await collector.on_response(
        httpx.Response(200, text="data: [DONE]", headers={"Content-Type": "text/event-stream"})
    )
    assert len(collector.records) == 2
    await collector.on_response(httpx.Response(200, json={"usage": {"prompt_tokens": 100}}))
    assert collector.totals() == {"cache_hit_tokens": None, "cache_miss_tokens": None}
