import json
from datetime import date, timedelta

import duckdb

from app.guardrails.sql import validate_sql
from scripts.criteo_tasks import make_expanded_tasks
from scripts.evaluate import same_results
from scripts.prepare_criteo import SCHEMA


def test_expanded_suite_oracles_isolation_and_edge_cases() -> None:
    # Independent small fixture: missing periods, zero clicks and unequal daily weights.
    rows = [
        {
            "date": str(date(2000, 1, 1) + timedelta(days=day)),
            "campaign_id": campaign,
            "impressions": 10 + day,
            "clicks": 0 if campaign % 3 == 0 else day % 7,
            "spend": (day + campaign) / 1000,
        }
        for day in range(31)
        for campaign in range(100, 190)
        if not (campaign % 4 == 0 and day > 20)
    ]
    excluded = set(range(100, 110))
    tasks = make_expanded_tasks(rows, excluded)
    assert len(tasks) == len({t["id"] for t in tasks}) == len({t["question"] for t in tasks}) == 200
    assert len({t["family"] for t in tasks}) == 20
    groups = {
        split: {c for t in tasks if t["split"] == split for c in t["campaign_ids"]}
        for split in ("dev", "test")
    }
    assert len(groups["dev"]) == 24 and len(groups["test"]) == 56
    assert not groups["dev"] & groups["test"]
    assert not (groups["dev"] | groups["test"]) & excluded
    assert {t["window_days"] for t in tasks} == {3, 4, 5, 7, 10}
    assert tasks == make_expanded_tasks(rows, excluded)
    with duckdb.connect() as db:
        db.execute(
            "CREATE TABLE daily_metrics(date DATE,campaign_id INTEGER,impressions BIGINT,clicks BIGINT,spend DOUBLE)"
        )
        db.executemany(
            "INSERT INTO daily_metrics VALUES (?,?,?,?,?)",
            [
                tuple(r[k] for k in ("date", "campaign_id", "impressions", "clicks", "spend"))
                for r in rows
            ],
        )
        for task in tasks:
            checked = validate_sql(task["oracle_sql"], schema=SCHEMA)
            cursor = db.execute(checked.normalized)
            assert cursor.description
            actual = [
                dict(zip([c[0] for c in cursor.description], row, strict=True))
                for row in cursor.fetchall()
            ]
            actual = json.loads(json.dumps(actual, default=str))
            assert same_results(actual, task["expected_rows"], task["ordered"]), task["id"]
        # Reproduce the model failure: correct output shape with the date filter omitted.
        task = next(t for t in tasks if t["family"] == "date_coverage")
        campaign_ids = ",".join(map(str, task["campaign_ids"]))
        cursor = db.execute(
            f"SELECT campaign_id,MIN(date) AS first_date,MAX(date) AS last_date "
            f"FROM daily_metrics WHERE campaign_id IN ({campaign_ids}) "
            "GROUP BY campaign_id ORDER BY campaign_id"
        )
        assert cursor.description
        unscoped = [
            dict(zip([c[0] for c in cursor.description], row, strict=True))
            for row in cursor.fetchall()
        ]
        unscoped = json.loads(json.dumps(unscoped, default=str))
        assert not same_results(unscoped, task["expected_rows"], task["ordered"])
    assert all(
        t["expected_rows"] == [{"cpc": None}] for t in tasks if t["family"] == "zero_denominator"
    )
    assert any(
        any(r.get("click_growth", 1) is None for r in t["expected_rows"])
        for t in tasks
        if t["family"] == "click_growth"
    )
