import json
import threading
import time
from decimal import Decimal
from functools import partial
from typing import Any

from app.agent.reliability import cancellation
from app.domain.models import AppError, ErrorCode, Evidence, Metrics, QueryData, ToolResult
from app.guardrails.sql import validate_sql
from app.settings import Settings
from app.tools import database
from app.tools.cache import QueryCache


class SQLExecutor:
    def __init__(self, settings: Settings, *, schema: dict[str, dict[str, str]] | None = None):
        self.settings = settings
        self.schema = schema
        # Resolve exactly once. Model/tool arguments cannot select a database file.
        self.path = settings.database_path.resolve()
        self.cache = QueryCache(settings)

    def execute(self, sql: str) -> ToolResult[QueryData]:
        validated = validate_sql(sql, self.settings.max_query_rows, schema=self.schema)
        # Output bounds participate in cache identity; different policies cannot reuse a broad result.
        identity = f"{validated.query_hash}:{self.settings.max_result_bytes}"
        return self.cache.get_or_compute(identity, lambda: self._execute(sql))

    def _execute(self, sql: str) -> ToolResult[QueryData]:
        validated = validate_sql(sql, self.settings.max_query_rows, schema=self.schema)
        started = time.perf_counter()
        db: Any = None
        timer: threading.Timer | None = None
        timed_out = threading.Event()
        scope = cancellation.get()
        if scope:
            scope.check()
        try:
            db = database.connect(self.settings)
            interrupt_db = (
                db.interrupt
                if self.settings.database_backend == "duckdb"
                else partial(db.cancel_safe, timeout=self.settings.database_connect_timeout_seconds)
            )
            if scope:
                scope.register(interrupt_db)

            def interrupt() -> None:
                timed_out.set()
                interrupt_db()

            timer = threading.Timer(self.settings.query_timeout_seconds, interrupt)
            timer.daemon = True
            timer.start()
            result = db.execute(
                database.query_sql(validated.normalized, self.settings.database_backend)
            )
            description = result.description
            assert description is not None
            columns = [str(item[0]) for item in description]
            types = [str(item[1]) for item in description]
            values = result.fetchmany(validated.max_rows + 1)
            if len(values) > validated.max_rows:
                raise AppError(ErrorCode.EXECUTION, "查询结果行数超限")
            if len(set(columns)) != len(columns):
                raise AppError(ErrorCode.EXECUTION, "结果列名称重复，请使用唯一别名")
            rows = [dict(zip(columns, row, strict=True)) for row in values]
            encoded = json.dumps(
                rows,
                default=lambda value: float(value) if isinstance(value, Decimal) else str(value),
                ensure_ascii=False,
                allow_nan=False,
            )
            if len(encoded.encode()) > self.settings.max_result_bytes:
                raise AppError(ErrorCode.EXECUTION, "查询结果大小超限，请缩小范围")
            rows = json.loads(encoded)
            data = QueryData(
                columns=columns,
                column_types=types,
                rows=rows,
                row_count=len(rows),
                query_hash=validated.query_hash,
                normalized_sql=validated.normalized,
                truncated=len(rows) == validated.max_rows,
            )
            evidence = Evidence(
                kind="query",
                query_hash=validated.query_hash,
                row_count=len(rows),
                summary=json.dumps(rows[:5], ensure_ascii=False),
            )
            return ToolResult(
                status="ok",
                data=data,
                evidence=[evidence],
                metrics=Metrics(duration_ms=(time.perf_counter() - started) * 1000),
            )
        except AppError:
            raise
        except Exception as exc:
            if scope:
                scope.check()
            if timed_out.is_set() or getattr(exc, "sqlstate", None) == "57014":
                raise AppError(ErrorCode.QUERY_TIMEOUT, "查询超时，请缩小范围", True) from None
            raise AppError(
                ErrorCode.EXECUTION, "只读查询执行失败，请检查字段、数据及表达式"
            ) from None
        finally:
            if timer:
                timer.cancel()
                timer.join()
            if db is not None:
                if scope:
                    scope.unregister(interrupt_db)
                db.close()
