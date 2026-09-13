import subprocess
from pathlib import Path

import pytest

from scripts.check_repository import (
    credential_values,
    git,
    inspect_content,
    inspect_file,
    main,
    scan,
    scan_history,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    return tmp_path


def test_git_ignores_local_files_but_keeps_public_inputs(repository: Path) -> None:
    private = [
        ".env",
        ".env.production",
        ".venv/config",
        "secrets/key.json",
        "credentials-development.json",
        "compose.override.yaml",
        "config.local.toml",
        "data/customer-export.csv",
        "data/generated/state.duckdb",
        "data/indexes/index.json",
        "state.sqlite3-wal",
        "nested/state.db-journal",
        "exports/customer.csv",
        "backups/snapshot.zip",
        "artifacts/trace.json",
        "logs/server.log",
        "browser.har",
        "开发路线图.md",
        "开发路线.md",
        "计划.md",
        "project.md",
    ]
    public = [
        ".env.example",
        "uv.lock",
        "compose.yaml",
        "data/schema.sql",
        "data/seeds/config.json",
        "data/knowledge/metrics.md",
        "data/eval/regression.jsonl",
        "docs/preview.png",
    ]
    result = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "--no-index", "-z", "--stdin"],
        input="\0".join(private + public).encode() + b"\0",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert {name.decode() for name in result.stdout.split(b"\0") if name} == set(private)


def test_force_added_private_file_is_rejected(repository: Path) -> None:
    (repository / ".env").write_text("MODEL_API_KEY=\n")
    git(repository, "add", "-f", ".env")
    assert scan(repository) == {".env": {"local_only_file"}}


def test_staged_secret_is_detected_after_working_copy_is_cleaned(repository: Path) -> None:
    path = repository / "notes.md"
    path.write_bytes(b"sk-" + b"a" * 32)
    git(repository, "add", "notes.md")
    path.write_text("sanitized\n")
    assert scan(repository) == {"notes.md": {"api_token"}}
    assert scan(repository, worktree=True) == {}


def test_configured_secret_is_found_without_printing_it(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "local-" + "opaque-value-for-test"
    (repository / ".env").write_text(f"APPROVAL_API_KEY='{secret}' # local only\n")
    (repository / "notes.md").write_text(secret)
    git(repository, "add", "notes.md")
    monkeypatch.chdir(repository)
    monkeypatch.setattr("sys.argv", ["check_repository"])
    with pytest.raises(SystemExit, match="1"):
        main()
    output = capsys.readouterr().out
    assert "notes.md: configured_local_secret" in output
    assert secret not in output


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"-----BEGIN " + b"PRIVATE KEY-----", "private_key"),
        (b"ghp_" + b"x" * 36, "github_token"),
        (b"AKIA" + b"A" * 16, "aws_access_key"),
        (b"/home/" + b"private-user/project", "personal_home_path"),
        (b"postgresql://user:" + b"real-password@example.invalid/db", "credential_in_url"),
        (b"SQLite format 3\x00", "database_content"),
        (b"\x00" * 8 + b"DUCK", "database_content"),
    ],
)
def test_secret_signatures_and_disguised_databases(content: bytes, reason: str) -> None:
    assert reason in inspect_content(content, ())


def test_public_example_requires_blank_credentials_but_allows_token_limits() -> None:
    assert not inspect_file(".env.example", b"MODEL_MAX_TOKENS=4096\nMODEL_API_KEY=\n", ())
    assert inspect_file(".env.example", b"MODEL_API_KEY=not-blank\n", ()) == {
        "example_contains_credential_value"
    }
    assert credential_values(
        '# API_KEY=comment\nexport AUTH_TOKEN="opaque" # comment\nPASSWORD= # blank\n'
    ) == ("opaque",)


def test_explicit_demo_connection_remains_allowed() -> None:
    assert not inspect_content(b"postgresql://reader:local-demo-reader@postgres/demo", ())


def test_dangling_symlinks_are_rejected(repository: Path) -> None:
    (repository / "notes.md").symlink_to("missing-secret")
    assert scan(repository, worktree=True) == {"notes.md": {"symlink_requires_review"}}
    git(repository, "add", "notes.md")
    assert scan(repository) == {"notes.md": {"symlink_requires_review"}}


def test_history_scan_finds_a_secret_removed_from_current_files(repository: Path) -> None:
    path = repository / "notes.md"
    path.write_bytes(b"sk-" + b"b" * 32)
    git(repository, "add", "notes.md")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    path.write_text("sanitized\n")
    git(repository, "add", "notes.md")
    assert scan(repository) == {}
    findings = scan_history(repository)
    assert len(findings) == 1
    assert next(iter(findings)).endswith(":notes.md")
    assert next(iter(findings.values())) == {"api_token"}


def test_history_checks_private_paths_even_when_blob_is_shared(repository: Path) -> None:
    for name in ("public.md", "project.md"):
        (repository / name).write_text("identical content\n")
    git(repository, "add", "public.md")
    git(repository, "add", "-f", "project.md")
    commit_options = (
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
    )
    git(repository, *commit_options, "commit", "--quiet", "-m", "local planning")
    findings = scan_history(repository)
    assert len(findings) == 1
    assert next(iter(findings)).endswith(":project.md")
    assert next(iter(findings.values())) == {"historical_local_only_file"}
    git(repository, "checkout", "--orphan", "release")
    git(repository, "rm", "--cached", "project.md")
    git(repository, *commit_options, "commit", "--quiet", "-m", "public candidate")
    assert scan_history(repository, "HEAD") == {}
    assert len(scan_history(repository)) == 1
    assert (repository / "project.md").is_file()
