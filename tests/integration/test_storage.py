import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.observability.context import event_sink
from app.settings import Settings
from app.tools.cache import QueryCache
from app.tools.sql import SQLExecutor
from scripts.evaluate import load_tasks, same_results


@pytest.mark.storage
def test_postgres_contract_and_permissions(settings: Settings) -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set TEST_POSTGRES_DSN to a seeded isolated read-only PostgreSQL database")
    import psycopg

    pg = SQLExecutor(
        settings.model_copy(update={"database_backend": "postgres", "postgres_dsn": SecretStr(dsn)})
    )
    local = SQLExecutor(settings)
    queries = [
        task.expected["oracle_sql"]
        for task in load_tasks(Path("data/eval/regression.jsonl"))
        if task.category == "sql"
    ]
    queries += [
        "SELECT campaign_id,COUNT(*) AS n,SUM(spend)/NULLIF(SUM(clicks),0) AS cpc FROM daily_metrics GROUP BY campaign_id ORDER BY campaign_id",
        "SELECT m.campaign_id,c.campaign_name,SUM(m.spend) AS spend FROM daily_metrics m JOIN campaigns c ON m.campaign_id=c.campaign_id GROUP BY m.campaign_id,c.campaign_name",
        "SELECT date,SUM(conversions) AS conversions FROM daily_metrics WHERE date > '2027-01-01' GROUP BY date",
    ]
    for query in queries:
        left, right = local.execute(query).data, pg.execute(query).data
        assert left and right and same_results(right.rows, left.rows), query
        assert left.query_hash == right.query_hash
    with psycopg.connect(dsn) as db:
        assert db.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        db.commit()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute("UPDATE daily_metrics SET spend=0")
        db.rollback()
        db.read_only = False
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("UPDATE daily_metrics SET spend=0")


@pytest.mark.storage
def test_redis_cache_singleflight_ttl_and_failure(settings: Settings) -> None:
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_REDIS_URL to an isolated Redis")
    config = settings.model_copy(
        update={
            "redis_url": SecretStr(url),
            "cache_namespace": "test:" + uuid4().hex,
            "cache_ttl_seconds": 1,
        }
    )
    cache = QueryCache(config)
    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return SQLExecutor(settings).execute("SELECT COUNT(*) AS n FROM daily_metrics")

    try:
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: cache.get_or_compute("count", compute), range(6)))
        assert calls == 1 and all(value.data.rows == [{"n": 4320}] for value in results)
        time.sleep(1.05)
        cache.get_or_compute("count", compute)
        assert calls == 2
        assert QueryCache(config.model_copy(update={"cache_namespace": "next:v3"})).key(
            "sql", "count"
        ) != cache.key("sql", "count")
    finally:
        cache.close()


def test_cache_outage_does_not_block_query(settings: Settings) -> None:
    pytest.importorskip("redis")
    config = settings.model_copy(
        update={"redis_url": SecretStr("redis://127.0.0.1:1/0"), "redis_timeout_seconds": 0.01}
    )
    events = []
    token = event_sink.set(lambda event, data: events.append(event))
    executor = SQLExecutor(config)
    try:
        assert executor.execute("SELECT COUNT(*) AS n FROM daily_metrics").data.rows == [
            {"n": 4320}
        ]
        assert "cache_degraded" in events
    finally:
        event_sink.reset(token)
        executor.cache.close()
