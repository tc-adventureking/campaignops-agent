import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.models import TERMINAL, AppError, ErrorCode, RunResult, RunStatus, TraceEvent


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if re.search(r"api.?key|authorization|password|secret|token$", str(key), re.I)
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        value = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", value)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
        return value
    return value


class RunStore:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()):
        self.path = path
        self.secrets = secrets
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("campaignops.trace")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, trace_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (run_id TEXT NOT NULL REFERENCES runs(run_id), event_id INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, event_id));
                CREATE TABLE IF NOT EXISTS idempotency (key_hash TEXT PRIMARY KEY, request_hash TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id));""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(
        self,
        db: sqlite3.Connection,
        run: RunResult,
        event: str,
        data: dict[str, Any],
        span_id: str | None = None,
    ) -> TraceEvent:
        event_id = db.execute(
            "SELECT COALESCE(MAX(event_id), 0) + 1 FROM events WHERE run_id=?", [run.run_id]
        ).fetchone()[0]
        safe_data = redact(data, self.secrets)
        item = TraceEvent(
            event_id=event_id,
            run_id=run.run_id,
            trace_id=run.trace_id,
            span_id=span_id,
            parent_span_id=run.root_span_id if span_id else None,
            timestamp=datetime.now(UTC),
            event=event,
            data=safe_data,
        )
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?)", [run.run_id, event_id, item.model_dump_json()]
        )
        self.logger.info(
            json.dumps(
                {
                    "timestamp": item.timestamp.isoformat(),
                    "level": "ERROR" if event == "error" else "INFO",
                    "trace_id": run.trace_id,
                    "span_id": span_id,
                    "module": "workflow",
                    "event": event,
                    "duration_ms": safe_data.get("duration_ms", 0),
                },
                ensure_ascii=False,
            )
        )
        return item

    def lookup_key(self, key: str, request_hash: str) -> RunResult | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT request_hash, run_id FROM idempotency WHERE key_hash=?",
                [hashlib.sha256(key.encode()).hexdigest()],
            ).fetchone()
        if not row:
            return None
        if row[0] != request_hash:
            raise AppError(ErrorCode.CONFLICT, "幂等键已用于不同请求，请使用新键")
        return self.get(row[1])

    def create(
        self,
        mode: str,
        key: str | None = None,
        request_hash: str = "",
        experiment_id: str | None = None,
    ) -> RunResult:
        now = datetime.now(UTC)
        run = RunResult(
            run_id=str(uuid4()),
            trace_id="trace_" + uuid4().hex,
            root_span_id="span_" + uuid4().hex,
            experiment_id=experiment_id,
            status=RunStatus.QUEUED,
            mode=mode,
            created_at=now,
            updated_at=now,
        )
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                existing = self.lookup_key(key, request_hash)
                if existing:
                    return existing
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?)",
                [run.run_id, run.trace_id, run.model_dump_json()],
            )
            if key:
                db.execute(
                    "INSERT INTO idempotency VALUES (?, ?, ?)",
                    [hashlib.sha256(key.encode()).hexdigest(), request_hash, run.run_id],
                )
            self._event(db, run, "status", {"status": run.status, "mode": mode})
        return run

    def get(self, run_id: str) -> RunResult:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE run_id=?", [run_id]).fetchone()
        if not row:
            raise AppError(ErrorCode.NOT_FOUND, "任务不存在")
        return RunResult.model_validate_json(row[0])

    def emit(
        self, run_id: str, event: str, data: dict[str, Any], span_id: str | None = None
    ) -> None:
        with self.lock, self._connect() as db:
            run = self.get(run_id)
            if run.status in TERMINAL:
                raise AppError(ErrorCode.CONFLICT, "终态不可添加执行事件")
            self._event(db, run, event, data, span_id)

    def transition(self, run_id: str, status: RunStatus, **updates: Any) -> RunResult:
        allowed = {
            RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
            RunStatus.RUNNING: TERMINAL,
        }
        with self.lock, self._connect() as db:
            run = self.get(run_id)
            if status not in allowed.get(run.status, set()):
                raise AppError(ErrorCode.CONFLICT, "不允许此状态转换，终态不可覆盖")
            values = (
                run.model_dump(mode="json")
                | {"status": status, "updated_at": datetime.now(UTC).isoformat()}
                | updates
            )
            run = RunResult.model_validate(redact(values, self.secrets))
            db.execute("UPDATE runs SET payload=? WHERE run_id=?", [run.model_dump_json(), run_id])
            if status in TERMINAL:
                self._event(
                    db,
                    run,
                    "error" if status == RunStatus.FAILED else "result",
                    run.model_dump(mode="json"),
                )
            self._event(db, run, "status", {"status": status})
        return run

    def events(self, run_id: str, after: int = 0) -> list[TraceEvent]:
        self.get(run_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM events WHERE run_id=? AND event_id>? ORDER BY event_id",
                [run_id, after],
            ).fetchall()
        return [TraceEvent.model_validate_json(row[0]) for row in rows]

    def export_trace(self, trace_id: str) -> list[TraceEvent]:
        with self._connect() as db:
            row = db.execute("SELECT run_id FROM runs WHERE trace_id=?", [trace_id]).fetchone()
        if not row:
            raise AppError(ErrorCode.NOT_FOUND, "Trace 不存在")
        return self.events(row[0])

    def list_runs(self, limit: int = 200) -> list[RunResult]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM runs ORDER BY json_extract(payload,'$.created_at') DESC LIMIT ?",
                [max(1, min(limit, 500))],
            ).fetchall()
        return [RunResult.model_validate_json(row[0]) for row in rows]

    def purge(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT run_id FROM runs WHERE json_extract(payload,'$.updated_at') < ? AND json_extract(payload,'$.status') NOT IN ('queued','running')",
                    [cutoff],
                )
            ]
            for identifier in ids:
                db.execute("DELETE FROM idempotency WHERE run_id=?", [identifier])
                db.execute("DELETE FROM events WHERE run_id=?", [identifier])
                db.execute("DELETE FROM runs WHERE run_id=?", [identifier])
        return len(ids)

    def recover_interrupted(self) -> None:
        with self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT run_id FROM runs").fetchall()]
        for run_id in ids:
            if self.get(run_id).status not in TERMINAL:
                self.transition(
                    run_id,
                    RunStatus.FAILED,
                    error={
                        "code": ErrorCode.SYSTEM,
                        "message": "进程重启中断任务，请重新提交",
                        "retryable": True,
                    },
                )
