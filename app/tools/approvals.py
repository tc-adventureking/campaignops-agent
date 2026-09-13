"""One-time approval and reversible simulation. This module has no platform/network client."""

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from app.domain.models import AppError, Contract, ErrorCode
from app.observability.store import RunStore
from app.settings import Settings


class BudgetChange(Contract):
    campaign_id: int = Field(ge=1, le=6)
    new_budget: float = Field(ge=0, le=1_000_000, allow_inf_nan=False, multiple_of=0.01)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Decision(Contract):
    decision: Literal["approve", "reject"]
    parameters_hash: str


class Execution(Contract):
    execution_token: str = Field(min_length=20, max_length=200)
    parameters: BudgetChange


class ApprovalService:
    def __init__(self, settings: Settings, store: RunStore):
        self.settings, self.store = settings, store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approval_audit (id INTEGER PRIMARY KEY, approval_id TEXT NOT NULL REFERENCES approvals(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS simulated_budgets (campaign_id INTEGER PRIMARY KEY, amount REAL NOT NULL, revision INTEGER NOT NULL);
            """)
            db.executemany(
                "INSERT OR IGNORE INTO simulated_budgets VALUES (?,1000,0)",
                [(i,) for i in range(1, 7)],
            )

    def authenticate(self, key: str | None) -> str:
        expected = self.settings.approval_api_key.get_secret_value()
        if not expected or not key or not hmac.compare_digest(key, expected):
            raise AppError(ErrorCode.FORBIDDEN, "审批口令无效或尚未启用模拟执行")
        return self.settings.approval_actor

    def _load(self, db: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        row = db.execute("SELECT payload FROM approvals WHERE id=?", [identifier]).fetchone()
        if not row:
            raise AppError(ErrorCode.NOT_FOUND, "审批提议不存在")
        return dict(json.loads(row[0]))

    def _save(self, db: sqlite3.Connection, value: dict[str, Any], event: str) -> None:
        db.execute(
            "INSERT INTO approvals VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            [value["id"], json.dumps(value, ensure_ascii=False)],
        )
        db.execute(
            "INSERT INTO approval_audit(approval_id,payload) VALUES (?,?)",
            [
                value["id"],
                json.dumps(
                    {
                        "event": event,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "actor": value["actor"],
                        "parameters_hash": value["parameters_hash"],
                        "summary": value["summary"],
                        "status": value["status"],
                        "run_id": value["run_id"],
                    },
                    ensure_ascii=False,
                ),
            ],
        )

    def propose(self, change: BudgetChange, run_id: str | None = None) -> dict[str, Any]:
        with self.store.lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            amount, revision = db.execute(
                "SELECT amount,revision FROM simulated_budgets WHERE campaign_id=?",
                [change.campaign_id],
            ).fetchone()
            value = {
                "id": str(uuid4()),
                "status": "proposed",
                "actor": self.settings.approval_actor,
                "parameters": change.model_dump(),
                "parameters_hash": change.fingerprint(),
                "previous_budget": amount,
                "revision": revision,
                "run_id": run_id,
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=self.settings.approval_ttl_seconds)
                ).isoformat(),
                "summary": f"模拟 Campaign {change.campaign_id} 日预算：{amount:.2f} → {change.new_budget:.2f}；不影响真实投放。",
            }
            self._save(db, value, "proposed")
        return value

    def _mutate(
        self,
        identifier: str,
        operation: Callable[[sqlite3.Connection, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        expired = False
        with self.store.lock, self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            value = self._load(db, identifier)
            if value["status"] in {"proposed", "approved"} and datetime.fromisoformat(
                value["expires_at"]
            ) <= datetime.now(UTC):
                value["status"] = "expired"
                value.pop("token_hash", None)
                self._save(db, value, "expired")
                expired = True
            else:
                value = operation(db, value)
        if expired:
            raise AppError(ErrorCode.CONFLICT, "审批已过期，需重新提议")
        return self.public(value)

    @staticmethod
    def public(value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if key != "token_hash"}

    def get(self, identifier: str) -> dict[str, Any]:
        try:
            return self._mutate(identifier, lambda db, value: value)
        except AppError as exc:
            if exc.info.code != ErrorCode.CONFLICT:
                raise
            with self.store._connect() as db:
                return self.public(self._load(db, identifier))

    def decide(self, identifier: str, decision: Decision, actor: str) -> dict[str, Any]:
        issued: str | None = None

        def operation(db: sqlite3.Connection, value: dict[str, Any]) -> dict[str, Any]:
            nonlocal issued
            if value["status"] != "proposed" or value["actor"] != actor:
                raise AppError(ErrorCode.CONFLICT, "审批状态或操作者不匹配")
            if not hmac.compare_digest(value["parameters_hash"], decision.parameters_hash):
                raise AppError(ErrorCode.CONFLICT, "参数已变化，需重新提议并审批")
            value["status"] = "approved" if decision.decision == "approve" else "rejected"
            if decision.decision == "approve":
                issued = secrets.token_urlsafe(32)
                value["token_hash"] = hashlib.sha256(issued.encode()).hexdigest()
            self._save(db, value, value["status"])
            return value

        result = self._mutate(identifier, operation)
        if issued:
            result["execution_token"] = issued
        return result

    def execute(self, identifier: str, execution: Execution, actor: str) -> dict[str, Any]:
        def operation(db: sqlite3.Connection, value: dict[str, Any]) -> dict[str, Any]:
            if value["status"] != "approved" or value["actor"] != actor:
                raise AppError(ErrorCode.CONFLICT, "必须由绑定操作者使用有效审批执行一次")
            if not hmac.compare_digest(
                value["parameters_hash"], execution.parameters.fingerprint()
            ):
                raise AppError(ErrorCode.CONFLICT, "执行参数与审批不一致，需重新审批")
            if not hmac.compare_digest(
                value["token_hash"], hashlib.sha256(execution.execution_token.encode()).hexdigest()
            ):
                raise AppError(ErrorCode.FORBIDDEN, "一次性执行凭证无效")
            changed = db.execute(
                "UPDATE simulated_budgets SET amount=?,revision=revision+1 WHERE campaign_id=? AND revision=?",
                [
                    execution.parameters.new_budget,
                    execution.parameters.campaign_id,
                    value["revision"],
                ],
            ).rowcount
            if changed != 1:
                raise AppError(ErrorCode.CONFLICT, "模拟预算已被其他操作修改，需重新审批")
            value["status"] = "executed"
            value.pop("token_hash", None)
            self._save(db, value, "executed")
            return value

        return self._mutate(identifier, operation)

    def rollback_proposal(self, identifier: str) -> dict[str, Any]:
        value = self.get(identifier)
        if value["status"] != "executed":
            raise AppError(ErrorCode.CONFLICT, "只有已执行操作可提议回滚")
        return self.propose(
            BudgetChange(
                campaign_id=value["parameters"]["campaign_id"], new_budget=value["previous_budget"]
            ),
            value["run_id"],
        )

    def audit(self, identifier: str) -> list[dict[str, Any]]:
        self.get(identifier)
        with self.store._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM approval_audit WHERE approval_id=? ORDER BY id",
                    [identifier],
                )
            ]
