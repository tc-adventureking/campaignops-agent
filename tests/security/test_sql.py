import pytest

from app.domain.models import AppError
from app.guardrails.sql import validate_sql
from app.settings import Settings
from app.tools.sql import SQLExecutor

ATTACKS = [
    "DROP TABLE daily_metrics",
    "DELETE FROM daily_metrics",
    "UPDATE daily_metrics SET spend=0",
    "INSERT INTO daily_metrics VALUES (1)",
    "CREATE TABLE evil (x INT)",
    "ATTACH '/tmp/evil.db' AS evil",
    "COPY daily_metrics TO '/tmp/leak.csv'",
    "PRAGMA version",
    "SELECT spend FROM daily_metrics; DROP TABLE daily_metrics",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_parquet('https://evil.example/data')",
    "SELECT * FROM sqlite_scan('/tmp/x', 'users')",
    "SELECT table_name FROM information_schema.tables",
    "SELECT name FROM sqlite_master",
    "SELECT read_blob('/etc/passwd') FROM daily_metrics",
    "SELECT getenv('MODEL_API_KEY') FROM daily_metrics",
    "SELECT nonexistent FROM daily_metrics",
    "SELECT * FROM daily_metrics",
    "WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT 1 FROM x) SELECT * FROM x",
    "SELECT spend FROM daily_metrics UNION SELECT 1",
    "SELECT spend INTO evil FROM daily_metrics",
    "SELECT m.spend FROM daily_metrics m CROSS JOIN daily_metrics n",
    "SELECT spend FROM other.daily_metrics",
    "SELECT repeat('x', 1000000000) FROM daily_metrics",
    "SELECT spend FROM daily_metrics LIMIT -1",
    "SELECT spend FROM daily_metrics LIMIT (SELECT MAX(clicks) FROM daily_metrics)",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access=true",
    "EXPLAIN SELECT spend FROM daily_metrics",
    "SELECT query('SELECT 1') FROM daily_metrics",
    "WITH daily_metrics AS (SELECT 1 AS spend) SELECT spend FROM daily_metrics",
]


@pytest.mark.parametrize("sql", ATTACKS)
def test_rejects_dangerous_sql(sql: str) -> None:
    with pytest.raises(AppError):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(spend) AS spend FROM daily_metrics",
        "SELECT c.channel_name, SUM(m.spend) AS spend FROM daily_metrics m JOIN channels c ON m.channel_id=c.channel_id GROUP BY c.channel_name",
        "WITH totals AS (SELECT campaign_id, SUM(spend) AS cost FROM daily_metrics GROUP BY campaign_id) SELECT campaign_id, cost FROM totals ORDER BY cost DESC LIMIT 3",
        "SELECT date, SUM(clicks)/NULLIF(SUM(impressions),0) AS ctr FROM daily_metrics GROUP BY date ORDER BY date",
        "SELECT COUNT(*) AS rows FROM daily_metrics",
    ],
)
def test_legitimate_queries(settings: Settings, sql: str) -> None:
    result = SQLExecutor(settings).execute(sql)
    assert result.data and result.data.row_count > 0
    assert result.evidence[0].query_hash


def test_result_cap(settings: Settings) -> None:
    executor = SQLExecutor(settings.model_copy(update={"max_query_rows": 2}))
    result = executor.execute("SELECT date, spend FROM daily_metrics LIMIT 99999")
    assert result.data and result.data.row_count == 2 and result.data.truncated


def test_size_cap(settings: Settings) -> None:
    executor = SQLExecutor(settings.model_copy(update={"max_result_bytes": 100}))
    with pytest.raises(AppError, match="大小超限"):
        executor.execute("SELECT date, spend FROM daily_metrics LIMIT 100")


def test_timeout_interrupts_query(settings: Settings) -> None:
    executor = SQLExecutor(settings.model_copy(update={"query_timeout_seconds": 0.005}))
    with pytest.raises(AppError) as error:
        executor.execute(
            "SELECT SUM(a.spend * b.spend * c.spend) AS x FROM daily_metrics a JOIN daily_metrics b ON a.date=b.date JOIN daily_metrics c ON b.date=c.date"
        )
    assert error.value.info.code == "query_timeout"
