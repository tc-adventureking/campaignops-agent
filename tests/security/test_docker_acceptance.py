from pathlib import Path

import pytest

from scripts.check_repository import git
from scripts.smoke_docker import clean_environment, snapshot


def test_docker_acceptance_does_not_inherit_paid_model_or_compose_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "MODEL_API_KEY",
        "AGENT_MODE",
        "POSTGRES_DSN",
        "POSTGRES_PASSWORD",
        "APPROVAL_API_KEY",
        "REDIS_URL",
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
        "API_PORT",
    ):
        monkeypatch.setenv(name, "must-not-inherit")
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")
    environment = clean_environment()
    assert "must-not-inherit" not in environment.values()
    assert environment["DOCKER_HOST"].endswith("docker.sock")


def test_snapshot_preserves_source_without_local_environment_or_runtime_data(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / "repo", tmp_path / "snapshot"
    source.mkdir()
    git(source, "init", "--quiet")
    project = Path(__file__).resolve().parents[2]
    (source / ".gitignore").write_bytes((project / ".gitignore").read_bytes())
    (source / ".env").write_text("MODEL_API_KEY=opaque-local-credential\n")
    (source / "app.py").write_text("print('public source')\n")
    (source / "data" / "generated").mkdir(parents=True)
    (source / "data" / "generated" / "local-trace.json").write_text("local runtime")
    snapshot(source, target)
    assert (target / "app.py").read_bytes() == (source / "app.py").read_bytes()
    assert not (target / ".env").exists()
    assert not (target / "data" / "generated" / "local-trace.json").exists()
    assert (source / ".env").is_file()


def test_snapshot_refuses_forced_tracked_secret(tmp_path: Path) -> None:
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".env").write_text("MODEL_API_KEY=\n")
    git(tmp_path, "add", ".env")
    with pytest.raises(RuntimeError, match="privacy check failed"):
        snapshot(tmp_path, tmp_path / "snapshot")
