import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from scripts.build_large_eval import VERSION, partition_campaigns, refreshed_partitions
from scripts.criteo_tasks import scenario_tasks
from scripts.evaluate import same_results
from scripts.evaluate_criteo import make_tasks, reference_rows
from scripts.evaluate_large import summarize
from scripts.prepare_criteo import SCHEMA
from scripts.verify_large_eval import execute_rows


def test_campaign_partitions_are_disjoint_and_exclude_seen() -> None:
    rows = [{"campaign_id": i} for i in range(300)]
    parts = partition_campaigns(rows, set(range(10)))
    sets = [set(v) for v in parts.values()]
    assert len(set().union(*sets)) == 290
    assert sum(map(len, sets)) == 290
    assert not set().union(*sets) & set(range(10))
    assert parts == partition_campaigns(list(reversed(rows)), set(range(10)))


def test_refresh_uses_only_unseen_reserve_and_retires_inspected_test() -> None:
    old = partition_campaigns([{"campaign_id": i} for i in range(675)], set(range(90)))
    base = {
        "version": VERSION,
        "campaign_partitions": old,
        "excluded_previously_used_campaigns": list(range(90)),
    }
    parts, excluded = refreshed_partitions(base)
    assert parts["train"] == old["train"]
    assert parts["dev"] == old["dev"]
    assert parts["test"] == old["reserve"]
    assert excluded == set(range(90)) | set(old["test"])
    assert not set(parts["test"]) & (set(old["train"]) | set(old["dev"]) | set(old["test"]))
    assert parts["reserve"] == []
    with pytest.raises(ValueError, match="unused reserve"):
        refreshed_partitions({**base, "version": "campaignops-large-v2"})


@pytest.mark.parametrize(
    "days,top_k,threshold,active", [(1, 1, 0, 7), (2, 5, 1000, 1), (14, 2, 10, 5)]
)
def test_variable_scenario_oracles(days: int, top_k: int, threshold: int, active: int) -> None:
    rows = [
        {
            "date": str(date(2000, 1, 1) + timedelta(days=d)),
            "campaign_id": c,
            "impressions": 10 + d,
            "clicks": 0 if c == 3 else d % 5,
            "spend": (c + d) / 100,
        }
        for d in range(31)
        for c in range(1, 5)
        if not (c == 4 and d >= 20)
    ]
    tasks = scenario_tasks(
        rows,
        [1, 2, 3, 4],
        0,
        date(2000, 1, 30),
        window_days=days,
        end_override=date(2000, 1, 30),
        top_k=top_k,
        min_impressions=threshold,
        min_active_days=active,
    )
    with duckdb.connect() as db:
        db.execute(
            "CREATE TABLE daily_metrics(date DATE,campaign_id INT,impressions BIGINT,clicks BIGINT,spend DOUBLE)"
        )
        db.executemany(
            "INSERT INTO daily_metrics VALUES (?,?,?,?,?)",
            [tuple(r[k] for k in SCHEMA["daily_metrics"]) for r in rows],
        )
        for task in tasks:
            _, actual = execute_rows(db, task["oracle_sql"])
            assert same_results(actual, task["expected_rows"], task["ordered"]), task["id"]


@pytest.mark.parametrize(
    "count,passed,split,limit,expected",
    [
        (12000, 11940, "test", None, False),  # exactly 99.5%, not strictly above
        (12000, 11941, "test", None, True),
        (
            11999,
            11999,
            "test",
            None,
            False,
        ),  # unfinished cannot pass even with perfect attempted answers
        (200, 200, "test", 200, False),
        (12000, 12000, "dev", None, False),
    ],
)
def test_gate_requires_complete_real_test_and_strict_threshold(
    count: int, passed: int, split: str, limit: int | None, expected: bool
) -> None:
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE results(id TEXT PRIMARY KEY,family TEXT,passed INTEGER)")
        db.executemany(
            "INSERT INTO results VALUES (?,?,?)",
            [(str(i), "sample", int(i < passed)) for i in range(count)],
        )
        metadata = {
            "split": split,
            "limit": limit,
            "selected_tasks": 200 if limit else 12000,
            "full_split_count": 12000,
            "dataset_total": 110000,
        }
        report = summarize(db, metadata)
        assert report["strictly_above_99_5_gate"] == expected


def test_original_generators_keep_existing_frozen_tasks() -> None:
    # Optional local artifact regression; CI retains the deterministic fixture tests above.
    root = Path("data/external/criteo")
    report = Path("artifacts/criteo-expanded-verify/tasks.json")
    if not report.exists():
        pytest.skip("Local external data not installed")
    from scripts.criteo_tasks import make_expanded_tasks

    originals = json.loads(report.read_text())
    tasks = make_expanded_tasks(reference_rows(root), {t["campaign_id"] for t in make_tasks(root)})
    for old, new in zip(originals, tasks, strict=True):
        old.pop("expected_columns", None)
        assert old == new


@pytest.mark.asyncio
async def test_service_pause_preserves_failures_and_resume_only_calls_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.domain.models import AppError, ErrorCode, Metrics
    from scripts import evaluate_large
    from scripts.prepare_criteo import sha256, write_json

    dataset, proof, output = (tmp_path / name for name in ("dataset", "proof", "output"))
    dataset.mkdir()
    proof.mkdir()
    public = [
        {
            "id": str(i),
            "question": f"公开问题{i}",
            "start_date": "2000-01-01",
            "end_date": "2000-01-01",
            "campaign_id": None,
        }
        for i in range(12)
    ]
    labels = [
        {
            **q,
            "family": "test",
            "expected_rows": [{"clicks": 1}],
            "oracle_sql": "private oracle",
            "ordered": False,
        }
        for q in public
    ]
    for name, rows in (("questions", public), ("labels", labels)):
        (dataset / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    part = {"count": 12}
    for name in ("questions", "labels"):
        part.update(
            {f"{name}_file": f"{name}.jsonl", f"{name}_sha256": sha256(dataset / f"{name}.jsonl")}
        )
    write_json(
        dataset / "manifest.json",
        {
            "data_hash": "fixture",
            "total_samples": 110000,
            "splits": {"test": part},
            "scope": "synthetic harness test",
        },
    )
    write_json(
        proof / "report.json",
        {
            "all_oracles_passed": True,
            "size_gate_passed": True,
            "dataset_fingerprint": {"dataset_manifest_sha256": sha256(dataset / "manifest.json")},
        },
    )
    with sqlite3.connect(proof / "verification.sqlite3") as db:
        db.execute("CREATE TABLE records(id TEXT, split TEXT, columns_json TEXT, passed INTEGER)")
        db.executemany(
            "INSERT INTO records VALUES (?,?,?,?)",
            [(q["id"], "test", '["clicks"]', 1) for q in public],
        )
    monkeypatch.setattr(evaluate_large, "validate_data", lambda _: {"data_hash": "fixture"})
    monkeypatch.setattr(
        evaluate_large,
        "SQLExecutor",
        lambda *a, **kw: SimpleNamespace(cache=SimpleNamespace(close=lambda: None)),
    )
    calls = []

    class UnavailableRunner:
        def __init__(self, *args, **kwargs):
            self.metrics = Metrics()

        async def plan(self, question, context, evidence):
            assert question in {q["question"] for q in public}
            assert evidence == []
            calls.append(question)
            raise AppError(ErrorCode.MODEL_UNAVAILABLE, "synthetic service failure")

    monkeypatch.setattr(evaluate_large, "OpenAICompatibleRunner", UnavailableRunner)
    first = await evaluate_large.evaluate(
        dataset, tmp_path, proof, output, "test", "openai", None, 1
    )
    assert first["metrics"]["completed"] == first["metrics"]["failed"] == 8
    assert first["metrics"]["unattempted"] == 4
    assert not first["metrics"]["strictly_above_99_5_gate"]
    assert (output / "service_pause.json").exists()
    second = await evaluate_large.evaluate(
        dataset, tmp_path, proof, output, "test", "openai", None, 1
    )
    assert second["metrics"]["failed"] == 12
    assert len(calls) == len(set(calls)) == 12
