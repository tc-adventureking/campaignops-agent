import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.domain.models import AppError
from app.observability.store import RunStore
from app.settings import Settings
from app.tools.approvals import ApprovalService, BudgetChange, Decision, Execution


@pytest.fixture
def service(settings: Settings, tmp_path: Path) -> ApprovalService:
    return ApprovalService(settings, RunStore(tmp_path / "state.db"))


def approve(service, proposal):
    return service.decide(
        proposal["id"],
        Decision(decision="approve", parameters_hash=proposal["parameters_hash"]),
        "local-operator",
    )


def test_single_use_and_rollback(service) -> None:
    proposal = service.propose(BudgetChange(campaign_id=3, new_budget=1200))
    accepted = approve(service, proposal)
    execution = Execution(
        execution_token=accepted["execution_token"],
        parameters=BudgetChange(**proposal["parameters"]),
    )
    assert "execution_token" not in service.get(proposal["id"])
    with ThreadPoolExecutor(2) as pool:

        def perform(_):
            try:
                return service.execute(proposal["id"], execution, "local-operator")["status"]
            except AppError:
                return "blocked"

        assert sorted(pool.map(perform, range(2))) == ["blocked", "executed"]
    rollback = service.rollback_proposal(proposal["id"])
    assert rollback["parameters"]["new_budget"] == 1000
    token = approve(service, rollback)["execution_token"]
    service.execute(
        rollback["id"],
        Execution(execution_token=token, parameters=BudgetChange(**rollback["parameters"])),
        "local-operator",
    )
    assert [row["event"] for row in service.audit(proposal["id"])] == [
        "proposed",
        "approved",
        "executed",
    ]
    assert accepted["execution_token"] not in service.store.path.read_bytes().decode(
        errors="ignore"
    )


@pytest.mark.parametrize(
    "attack",
    ["unapproved", "actor", "token", "parameters", "replay", "stale", "expired", "rejected"],
)
def test_approval_attacks(service, attack) -> None:
    proposal = service.propose(BudgetChange(campaign_id=1, new_budget=1200))
    accepted = (
        approve(service, proposal)
        if attack not in {"unapproved", "rejected"}
        else {"execution_token": "invalid" * 6}
    )
    params = BudgetChange(campaign_id=1, new_budget=1300 if attack == "parameters" else 1200)
    execution = Execution(
        execution_token="wrong" * 6 if attack == "token" else accepted["execution_token"],
        parameters=params,
    )
    if attack in {"replay", "stale"}:
        service.execute(proposal["id"], execution, "local-operator")
        if attack == "stale":
            older = service.propose(BudgetChange(campaign_id=1, new_budget=1400))
            token = approve(service, older)["execution_token"]
            other = service.propose(BudgetChange(campaign_id=1, new_budget=1500))
            other_token = approve(service, other)["execution_token"]
            service.execute(
                other["id"],
                Execution(
                    execution_token=other_token, parameters=BudgetChange(**other["parameters"])
                ),
                "local-operator",
            )
            proposal = older
            execution = Execution(
                execution_token=token, parameters=BudgetChange(**older["parameters"])
            )
    if attack == "expired":
        with service.store._connect() as db:
            value = service._load(db, proposal["id"])
            value["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            db.execute(
                "UPDATE approvals SET payload=? WHERE id=?", [json.dumps(value), proposal["id"]]
            )
    if attack == "rejected":
        service.decide(
            proposal["id"],
            Decision(decision="reject", parameters_hash=proposal["parameters_hash"]),
            "local-operator",
        )
    with pytest.raises(AppError):
        service.execute(
            proposal["id"], execution, "imposter" if attack == "actor" else "local-operator"
        )
    if attack == "expired":
        assert service.audit(proposal["id"])[-1]["event"] == "expired"


def test_api_requires_operator_key(settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/approvals", json={"campaign_id": 1, "new_budget": 1200}).status_code
            == 403
        )
