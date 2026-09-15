import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from app.domain.models import AppError, Metrics, SQLPlan
from app.guardrails.sql import validate_sql
from scripts.evaluate import same_results
from scripts.evaluate_criteo import evaluate, make_tasks, validate_data
from scripts.prepare_criteo import SCHEMA, build, sha256


@pytest.fixture
def criteo_data(tmp_path: Path) -> Path:
    root = tmp_path / "criteo"
    (root / "raw").mkdir(parents=True)
    shard = root / "raw" / "0000.parquet"
    with duckdb.connect() as db:
        db.execute(
            "CREATE TABLE events(timestamp BIGINT,campaign INTEGER,click INTEGER,cost DOUBLE)"
        )
        db.executemany(
            "INSERT INTO events VALUES (?,?,?,?)",
            [
                (day * 86400, campaign, int(day % 3 == 0), 0.001 * (day + 1))
                for day in range(30)
                for campaign in range(100, 110)
                for _ in range(day + 1)
            ],
        )
        db.execute("COPY events TO ? (FORMAT PARQUET)", [str(shard)])
    build(
        root,
        {
            "dataset": "synthetic-test-fixture",
            "revision": "fixture",
            "license": "test",
            "source": "test",
        },
        [{"filename": shard.name, "sha256": sha256(shard)}],
    )
    return root


def test_complete_import_independent_oracle_and_disjoint_splits(criteo_data: Path) -> None:
    manifest = validate_data(criteo_data)
    assert manifest["raw_rows"] == 4650
    assert manifest["validation"]["groups_checked"] == 300
    tasks = make_tasks(criteo_data)
    assert len(tasks) == 40
    dev, test = ([t for t in tasks if t["split"] == split] for split in ("dev", "test"))
    assert not {t["family"] for t in dev} & {t["family"] for t in test}
    assert not {t["campaign_id"] for t in dev} & {t["campaign_id"] for t in test}
    with duckdb.connect(str(criteo_data / "campaignops.duckdb"), read_only=True) as db:
        for task in tasks:
            cursor = db.execute(task["oracle_sql"])
            assert cursor.description
            actual = [
                dict(zip([c[0] for c in cursor.description], row, strict=True))
                for row in cursor.fetchall()
            ]
            actual = json.loads(json.dumps(actual, default=str))
            assert same_results(actual, task["expected_rows"], task["ordered"])
        task = next(t for t in tasks if t["family"] == "ctr")
        wrong = db.execute(
            task["oracle_sql"].replace(
                "SUM(clicks)*1.0/NULLIF(SUM(impressions),0)",
                "AVG(clicks*1.0/NULLIF(impressions,0))",
            )
        ).fetchone()
        assert wrong is not None
        assert not same_results([{"ctr": wrong[0]}], task["expected_rows"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT conversion_value FROM daily_metrics",
        "SELECT channel_id FROM daily_metrics",
        "SELECT COUNT(*) FROM campaigns",
        "SELECT * FROM read_csv('/etc/passwd')",
        "DROP TABLE daily_metrics",
        "WITH a AS (SELECT SUM(clicks) AS n FROM daily_metrics), b AS (SELECT SUM(impressions) AS n FROM daily_metrics) SELECT a.n/b.n AS ctr FROM a,b",
        "WITH a AS (SELECT SUM(clicks) AS n FROM daily_metrics), b AS (SELECT SUM(impressions) AS n FROM daily_metrics) SELECT a.n/b.n AS ctr FROM a CROSS JOIN b",
    ],
)
def test_external_schema_does_not_relax_guardrails(sql: str) -> None:
    with pytest.raises(AppError):
        validate_sql(sql, schema=SCHEMA)


def test_rounded_period_difference_does_not_pass_numeric_contract() -> None:
    expected = [{"ctr_change_pp": -2.511926031479655}]
    assert not same_results([{"ctr_change_pp": -2.5119}], expected)
    assert same_results([{"ctr_change_pp": -2.51192603147965}], expected)


async def test_verify_is_not_model_accuracy_and_rejects_tampering(
    criteo_data: Path, tmp_path: Path
) -> None:
    report = await evaluate(criteo_data, tmp_path / "verify", "verify", "all", None, 2, "semantic")
    assert report["metrics"]["oracle_checks_passed"] == 40
    assert report["metrics"]["execution_accuracy"] is None
    assert (tmp_path / "verify" / "review.html").exists()
    with (criteo_data / "reference.csv").open("a") as handle:
        handle.write("tampered")
    with pytest.raises(ValueError, match="fingerprint"):
        validate_data(criteo_data)


@pytest.mark.parametrize(
    "outcome",
    ["correct", "wrong_value", "forbidden", "timeout", "empty_correct", "empty_wrong_columns"],
)
async def test_live_scoring_with_stubbed_model(
    criteo_data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    task = next(
        t
        for t in make_tasks(criteo_data)
        if (
            t["family"] == "empty_filter"
            if outcome.startswith("empty")
            else t["family"] == "totals"
        )
    )
    monkeypatch.setattr("scripts.evaluate_criteo.make_tasks", lambda root: [task])

    class Runner:
        metrics = Metrics()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def plan(self, *args: Any) -> SQLPlan:
            if outcome == "timeout":
                raise TimeoutError
            sql = task["oracle_sql"]
            if outcome == "wrong_value":
                sql = sql.replace("SUM(clicks)", "SUM(clicks)+1")
            if outcome == "forbidden":
                sql = "SELECT conversion_value FROM daily_metrics"
            if outcome == "empty_wrong_columns":
                sql = "SELECT clicks FROM daily_metrics WHERE impressions < 0"
            return SQLPlan(
                sql=sql,
                metrics=[],
                dimensions=[],
                assumptions=[],
                start_date=task["start_date"],
                end_date=task["end_date"],
            )

    monkeypatch.setattr("scripts.evaluate_criteo.OpenAICompatibleRunner", Runner)
    report = await evaluate(
        criteo_data, tmp_path / outcome, "openai", task["split"], 1, 1, "semantic"
    )
    assert report["metrics"]["passed"] == int(outcome in {"correct", "empty_correct"})
    assert report["results"][0]["oracle_verified"]
