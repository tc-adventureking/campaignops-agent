"""Check Git snapshots for local-only files and credentials without printing their contents."""

import argparse
import fnmatch
import re
import subprocess
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
PRIVATE_PARTS = {
    ".git",
    ".venv",
    "venv",
    "env",
    ".direnv",
    ".aws",
    ".ssh",
    ".azure",
    ".credentials",
    "secrets",
    "artifacts",
    "uploads",
    "exports",
    "dumps",
    "backups",
    "logs",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "test-results",
    "playwright-report",
}
PRIVATE_NAMES = {
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".python-version",
    "credentials.json",
    "compose.override.yaml",
    "compose.override.yml",
    "compose.local.yaml",
    "compose.local.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
}
PRIVATE_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.db",
    "*.db-*",
    "*.duckdb*",
    "*.sqlite*",
    "*.dump",
    "*.sql.gz",
    "*.sql.zip",
    "*.bak",
    "*.backup",
    "*.swp",
    "*.swo",
    "*~",
    "*.log",
    "*.har",
    "*.webm",
    "*.mp4",
    "credentials*.json",
    "service-account*.json",
    "*.local.toml",
)
SECRET_NAME = re.compile(
    r"api.?key|(?:^|_)token$|password|secret|private.?key|authorization|dsn|redis_url", re.I
)
SIGNATURES = (
    (
        "private_key",
        re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    ("api_token", re.compile(rb"\bsk-(?:proj-|ant-api03-)?[A-Za-z0-9_-]{20,}\b")),
    (
        "github_token",
        re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("personal_home_path", re.compile(rb"/(?:home|Users)/[^/\s\"'<>]+/")),
    ("personal_home_path", re.compile(rb"[A-Za-z]:[\\/]Users[\\/][^\\/\s\"'<>]+[\\/]")),
)
AUTH_URL = re.compile(rb"(?:https?|postgres(?:ql)?|redis)://[^\s/:]+:([^\s/@]+)@")


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def private_path(name: str) -> bool:
    path = Path(name)
    if any(part in PRIVATE_PARTS for part in path.parts):
        return True
    if path.name.startswith(".env") and path.name != ".env.example":
        return True
    if path.name in PRIVATE_NAMES or any(
        fnmatch.fnmatch(path.name, pattern) for pattern in PRIVATE_GLOBS
    ):
        return True
    if name in {"开发路线图.md", "开发路线.md", "计划.md", "project.md"}:
        return True
    if path.parts and path.parts[0] == "data":
        return name != "data/schema.sql" and not (
            len(path.parts) > 2 and path.parts[1] in {"knowledge", "eval", "seeds"}
        )
    return (
        name.startswith(("reports/", "docs/evaluation/", "docs/screenshots/"))
        or name == "docs/openapi.json"
    )


def credential_values(content: str) -> tuple[str, ...]:
    values = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        if not separator or not SECRET_NAME.search(key.strip()):
            continue
        value = value.strip()
        if value.startswith("#"):
            continue
        if value.startswith(('"', "'")):
            value = value[1:].partition(value[0])[0]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if value:
            values.append(value)
    return tuple(values)


def local_secrets(root: Path) -> tuple[bytes, ...]:
    values: set[bytes] = set()
    for path in root.glob(".env*"):
        if not path.is_file() or path.name == ".env.example":
            continue
        for value in credential_values(path.read_text(errors="replace")):
            if len(value) >= 8:
                values.add(value.encode())
    return tuple(values)


def inspect_content(content: bytes, known: tuple[bytes, ...]) -> set[str]:
    reasons = {label for label, pattern in SIGNATURES if pattern.search(content)}
    if any(value in content for value in known):
        reasons.add("configured_local_secret")
    if content.startswith(b"SQLite format 3\x00") or content[8:12] == b"DUCK":
        reasons.add("database_content")
    for match in AUTH_URL.finditer(content):
        password = match.group(1)
        # Explicit synthetic credentials used by isolated Compose/CI examples.
        if not password.startswith((b"local-demo-", b"ci-", b"test-", b"${", b"<")):
            reasons.add("credential_in_url")
    return reasons


def inspect_file(name: str, content: bytes, known: tuple[bytes, ...]) -> set[str]:
    reasons = inspect_content(content, known)
    if private_path(name):
        reasons.add("local_only_file")
    if len(content) > MAX_BYTES:
        reasons.add("file_over_10_mib")
    if Path(name).name == ".env.example":
        if credential_values(content.decode(errors="replace")):
            reasons.add("example_contains_credential_value")
    return reasons


def scan(root: Path, worktree: bool = False) -> dict[str, set[str]]:
    known = local_secrets(root)
    args = ("ls-files", "-co", "--exclude-standard", "-z") if worktree else ("ls-files", "-z")
    names = sorted({name.decode() for name in git(root, *args).split(b"\0") if name})
    findings: dict[str, set[str]] = {}
    for name in names:
        if worktree:
            path = root / name
            if path.is_symlink():
                findings[name] = {"symlink_requires_review"}
                continue
            if not path.exists():
                continue
            if path.stat().st_size > MAX_BYTES:
                findings[name] = {"file_over_10_mib"}
                continue
            content = path.read_bytes()
        else:
            # Inspect the index, even if the working copy was subsequently sanitized.
            if int(git(root, "cat-file", "-s", ":" + name)) > MAX_BYTES:
                findings[name] = {"file_over_10_mib"}
                continue
            if git(root, "ls-files", "-s", "--", name).startswith(b"120000"):
                findings[name] = {"symlink_requires_review"}
                continue
            content = git(root, "show", ":" + name)
        reasons = inspect_file(name, content, known)
        if reasons:
            findings[name] = reasons
    return findings


def scan_history(root: Path, revision: str = "--all") -> dict[str, set[str]]:
    known = local_secrets(root)
    findings: dict[str, set[str]] = {}
    checked: dict[bytes, set[str]] = {}
    for commit in git(root, "rev-list", revision).splitlines():
        for entry in git(root, "ls-tree", "-rz", commit.decode()).split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if kind != b"blob":
                continue
            if oid not in checked:
                checked[oid] = (
                    {"historical_large_blob_requires_review"}
                    if int(git(root, "cat-file", "-s", oid.decode())) > MAX_BYTES
                    else inspect_content(git(root, "cat-file", "blob", oid.decode()), known)
                )
            reasons = checked[oid].copy()
            if private_path(name.decode()):
                reasons.add("historical_local_only_file")
            if mode == b"120000":
                reasons.add("historical_symlink_requires_review")
            if reasons:
                findings[commit[:12].decode() + ":" + name.decode()] = reasons
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worktree", action="store_true", help="Scan tracked and unignored working files"
    )
    parser.add_argument(
        "--history", action="store_true", help="Also scan reachable historical paths and contents"
    )
    parser.add_argument(
        "--history-ref", help="Limit --history to this branch/commit (default: all refs)"
    )
    args = parser.parse_args()
    if args.history_ref and (not args.history or args.history_ref.startswith("-")):
        parser.error("--history-ref requires --history and a branch/commit name")
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    findings = scan(root, args.worktree)
    if args.history:
        findings.update(scan_history(root, args.history_ref or "--all"))
    for name, reasons in sorted(findings.items()):
        print(f"{name}: {', '.join(sorted(reasons))}")
    if findings:
        print(
            "Repository privacy check failed. File contents and secret values are intentionally omitted."
        )
        raise SystemExit(1)
    print("Repository privacy check passed (no configured secrets or supported signatures found).")


if __name__ == "__main__":
    main()
