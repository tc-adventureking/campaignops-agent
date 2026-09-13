"""Install the versioned synthetic schema with an admin DSN, never in the API process."""

import hashlib
from pathlib import Path

import duckdb
import psycopg
import sqlglot
from psycopg import sql

from app.domain.semantics import SCHEMA
from app.settings import Settings


def migrate(settings: Settings) -> None:
    if (
        not settings.postgres_admin_dsn.get_secret_value()
        or not settings.postgres_reader_password.get_secret_value()
    ):
        raise ValueError("Configure POSTGRES_ADMIN_DSN and POSTGRES_READER_PASSWORD locally")
    source = Path("data/schema.sql").read_text()
    version = hashlib.sha256(source.encode()).hexdigest()
    with psycopg.connect(settings.postgres_admin_dsn.get_secret_value()) as db:
        db.execute("SELECT pg_advisory_xact_lock(2026092902)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        prior = db.execute("SELECT version FROM schema_migrations").fetchall()
        if prior and prior != [(version,)]:
            raise ValueError(
                "Schema differs from installed migration; add an explicit forward migration"
            )
        if not prior:
            for statement in sqlglot.transpile(source, read="duckdb", write="postgres"):
                db.execute(statement)
            with duckdb.connect(str(settings.database_path), read_only=True) as local:
                for table in SCHEMA:
                    # Schema order must respect foreign keys; SCHEMA preserves this order.
                    rows = local.execute(f'SELECT * FROM "{table}"').fetchall()
                    with db.cursor().copy(
                        sql.SQL("COPY {} FROM STDIN").format(sql.Identifier(table))
                    ) as copy:
                        for row in rows:
                            copy.write_row(row)
            db.execute("INSERT INTO schema_migrations(version) VALUES (%s)", [version])
        exists = db.execute("SELECT 1 FROM pg_roles WHERE rolname='campaignops_reader'").fetchone()
        if not exists:
            db.execute(
                "CREATE ROLE campaignops_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
            )
        db.execute(
            sql.SQL("ALTER ROLE campaignops_reader PASSWORD {}").format(
                sql.Literal(settings.postgres_reader_password.get_secret_value())
            )
        )
        db.execute("ALTER ROLE campaignops_reader SET default_transaction_read_only = on")
        db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        db.execute("GRANT USAGE ON SCHEMA public TO campaignops_reader")
        db.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM campaignops_reader")
        for table in SCHEMA:
            db.execute(
                sql.SQL("GRANT SELECT ON {} TO campaignops_reader").format(sql.Identifier(table))
            )
    print("PostgreSQL schema and synthetic dataset ready; API role is read-only.")


if __name__ == "__main__":
    migrate(Settings())
