"""Dialect/connection boundary. Models can never choose a DSN or SQL role."""

from typing import Any

import duckdb
import sqlglot

from app.settings import Settings


def connect(settings: Settings) -> Any:
    if settings.database_backend == "duckdb":
        return duckdb.connect(
            str(settings.database_path.resolve()),
            read_only=True,
            config={
                "enable_external_access": "false",
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "threads": "1",
                "memory_limit": "256MB",
            },
        )
    import psycopg

    db = psycopg.connect(
        settings.postgres_dsn.get_secret_value(),
        connect_timeout=settings.database_connect_timeout_seconds,
    )
    try:
        db.read_only = True
        db.execute("SET LOCAL search_path TO public, pg_catalog")
        db.execute(f"SET LOCAL statement_timeout = {int(settings.query_timeout_seconds * 1000)}")
        db.execute(f"SET LOCAL lock_timeout = {int(settings.query_timeout_seconds * 1000)}")
        # Refuse an accidentally supplied admin/write role before executing model SQL.
        privileged = db.execute(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if privileged and privileged[0]:
            raise ValueError("PostgreSQL requires the read-only application role")
        return db
    except BaseException:
        db.close()
        raise


def query_sql(canonical: str, backend: str) -> str:
    if backend == "duckdb":
        return canonical
    return sqlglot.transpile(canonical, read="duckdb", write="postgres")[0]
