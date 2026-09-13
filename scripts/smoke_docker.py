"""Build and accept both Compose modes with isolated source, ports and disposable volumes."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.check_repository import git, scan

CANARIES = (
    "data/knowledge/.env",
    "data/seeds/private.key",
    "data/customer-export.csv",
    "data/generated/must-not-copy.txt",
    "data/indexes/must-not-copy.txt",
)


def clean_environment() -> dict[str, str]:
    # Preserve transport/runtime configuration, never application or Compose overrides.
    names = {
        "PATH",
        "HOME",
        "USER",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "XDG_RUNTIME_DIR",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in names}


def snapshot(root: Path, target: Path) -> None:
    if scan(root, worktree=True):
        raise RuntimeError("Repository privacy check failed; run scripts.check_repository first")
    names = git(root, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
    for name in sorted(set(names) - {b""}):
        source = root / os.fsdecode(name)
        if source.is_file():
            destination = target / os.fsdecode(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    assert not (target / ".env").exists()
    # Fake private files prove Docker's data/context exclusions work during the actual build.
    for canary in CANARIES:
        path = target / canary
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-docker-exclusion-canary\n")


def request(base: str, path: str, body: Any = None, key: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers
    )
    # Local API requests must not pass through a host proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        req, timeout=30
    ) as response:
        content = response.read().decode()
        return (
            json.loads(content)
            if "application/json" in response.headers.get("Content-Type", "")
            else content
        )


def ready(base: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            request(base, "/health/ready")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    raise RuntimeError("Container API did not become ready")


def completed(base: str, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run: dict[str, Any] = request(base, "/v1/runs/" + run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.1)
    raise RuntimeError("Container run did not finish")


def container_checks(backend: str) -> dict[str, Any]:
    import importlib.metadata

    from app.settings import Settings
    from app.tools.sql import SQLExecutor

    settings = Settings()
    assert settings.agent_mode == "demo" and not settings.model_api_key.get_secret_value()
    assert settings.database_backend == backend
    assert not settings.postgres_admin_dsn.get_secret_value()
    assert not settings.postgres_reader_password.get_secret_value()
    assert not Path(".env").exists() and not Path(".git").exists()
    assert all(not Path(name).exists() for name in CANARIES)
    executor = SQLExecutor(settings)
    try:
        result = executor.execute("SELECT COUNT(*) AS n FROM daily_metrics")
        assert result.data and result.data.rows == [{"n": 4320}]
    finally:
        executor.cache.close()
    if backend == "postgres":
        import psycopg
        import redis

        with psycopg.connect(settings.postgres_dsn.get_secret_value()) as db:
            readonly = db.execute("SHOW default_transaction_read_only").fetchone()
            assert readonly and readonly[0] == "on"
            db.commit()
            try:
                db.execute("UPDATE daily_metrics SET spend=0")
            except psycopg.errors.ReadOnlySqlTransaction:
                db.rollback()
            else:
                raise AssertionError("PostgreSQL reader allowed a write")
            db.read_only = False
            try:
                db.execute("UPDATE daily_metrics SET spend=0")
            except psycopg.errors.InsufficientPrivilege:
                db.rollback()
            else:
                raise AssertionError("PostgreSQL reader owns write privileges")
        with redis.Redis.from_url(settings.redis_url.get_secret_value()) as cache:
            assert cache.ping()
    return {
        "database_rows": 4320,
        "backend": backend,
        "image_privacy": True,
        "reader_permissions": backend == "postgres",
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("duckdb", "psycopg", "redis", "openai", "youtu-agent")
        },
    }


def accept(root: Path, output: Path, *, reuse_build_cache: bool = False) -> None:
    output.mkdir(parents=True, exist_ok=True)
    env = clean_environment()
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker CLI is unavailable; install Docker Engine and Compose first")

    def run(args: list[str], cwd: Path, *, timeout: int = 1800) -> str:
        with (output / "commands.log").open("a") as log:
            log.write("\n$ docker " + " ".join(args) + "\n")
            log.flush()
            result = subprocess.run(
                [docker, *args],
                cwd=cwd,
                env=env,
                stdout=log
                if any(word in args for word in ("build", "up", "pull"))
                else subprocess.PIPE,
                stderr=log,
                text=True,
                timeout=timeout,
            )
            log.write(result.stdout or "")
        if result.returncode:
            raise RuntimeError(
                f"Docker command failed ({result.returncode}); see {output / 'commands.log'}"
            )
        return (result.stdout or "").strip()

    summary: dict[str, Any] = {
        "date": datetime.now(UTC).isoformat(),
        "passed": False,
        "reuse_build_cache": reuse_build_cache,
        "modes": {},
        "engine": json.loads(run(["version", "--format", "{{json .Server}}"], root)),
        "compose": run(["compose", "version", "--short"], root),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="campaignops-docker-") as temporary:
            workspace = Path(temporary)
            snapshot(root, workspace)
            for backend in ("duckdb", "postgres"):
                project = "campaignops-check-" + backend + "-" + uuid4().hex[:10]
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                env.update({"API_PORT": str(port), "DATABASE_BACKEND": backend})
                env["REDIS_URL"] = "redis://redis:6379/0" if backend == "postgres" else ""
                compose = ["compose", "--progress", "plain", "-p", project, "-f", "compose.yaml"]
                if backend == "postgres":
                    compose += ["--profile", "storage"]

                def dc(*args: str, prefix: tuple[str, ...] = tuple(compose)) -> str:
                    return run([*prefix, *args], workspace)

                base = f"http://127.0.0.1:{port}"
                record: dict[str, Any] = {"passed": False, "fresh_volumes": True}
                summary["modes"][backend] = record
                try:
                    print(f"[{backend}] Building and starting isolated Compose project", flush=True)
                    if backend == "duckdb" and not reuse_build_cache:
                        dc("build", "--pull", "--no-cache", "api")
                    dc("up", "--build", "--wait", "--wait-timeout", "180")
                    ready(base)
                    record["images"] = json.loads(dc("images", "--format", "json"))
                    record["container"] = json.loads(
                        dc(
                            "exec",
                            "-T",
                            "api",
                            "python",
                            "-m",
                            "scripts.smoke_docker",
                            "--inside",
                            "--backend",
                            backend,
                        )
                    )
                    assert request(base, "/v1/workspace")["mode"] == "demo"
                    assert "<html" in request(base, "/").lower()
                    assert "openapi" in request(base, "/openapi.json")
                    assert (
                        "<strong>"
                        in request(base, "/v1/markdown", {"markdown": "**Docker 验收**"})["html"]
                    )
                    body = {"question": "诊断 Campaign 3 最近7天 CVR 下滑的原因"}
                    key = "docker-check-" + uuid4().hex
                    created = request(base, "/v1/runs", body, key)
                    first = completed(base, created["run_id"])
                    assert first["status"] == "succeeded"
                    assert "event: result" in request(base, f"/v1/runs/{first['run_id']}/events")
                    for question, status in (
                        ("诊断 Campaign 6 最近7天渠道成本异常", "succeeded"),
                        ("将 Campaign 3 预算提高 20%", "approval_required"),
                    ):
                        created = request(base, "/v1/runs", {"question": question})
                        assert completed(base, created["run_id"])["status"] == status
                    record["http_sse_markdown_cases"] = True
                    print(f"[{backend}] Running all 100 regression tasks", flush=True)
                    dc("exec", "-T", "api", "python", "-m", "scripts.evaluate", "--mode", "demo")
                    report = json.loads(
                        dc("exec", "-T", "api", "cat", "artifacts/eval/report.json")
                    )
                    assert report["metadata"]["database_backend"] == backend
                    assert report["metadata"]["tasks"] == 100
                    assert (
                        report["metrics"]["task_success"] == 1
                        and report["regression_gate"]["passed"]
                    )
                    record["regression"] = {
                        name: report[name] for name in ("metadata", "metrics", "regression_gate")
                    }
                    dc("cp", "api:/app/artifacts/eval", str(output / backend))
                    print(f"[{backend}] Verifying restart persistence", flush=True)
                    dc("restart", "api")
                    ready(base)
                    assert request(base, "/v1/runs", body, key)["run_id"] == first["run_id"]
                    assert request(base, "/v1/runs/" + first["run_id"])["status"] == "succeeded"
                    record["restart_idempotency"] = True
                    if backend == "postgres":
                        print("[postgres] Verifying Redis outage fallback", flush=True)
                        dc("stop", "redis")
                        created = request(base, "/v1/runs", body)
                        degraded = completed(base, created["run_id"])
                        assert degraded["status"] == "succeeded"
                        trace = request(base, "/v1/traces/" + degraded["trace_id"])
                        assert any(event["event"] == "cache_degraded" for event in trace)
                        record["redis_outage_fallback"] = True
                    record["passed"] = True
                finally:
                    try:
                        dc("logs", "--no-color")
                    finally:
                        dc("down", "--volumes", "--remove-orphans", "--rmi", "local")
                        record["cleaned_up"] = True
            summary["passed"] = True
    finally:
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        )
    print(
        f"Docker acceptance passed: both backends 100/100. Report: {output / 'summary.json'}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/docker") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--reuse-build-cache",
        action="store_true",
        help="Reuse build layers for a repeat check; containers and volumes remain fresh",
    )
    parser.add_argument("--backend", choices=["duckdb", "postgres"], default="duckdb")
    args = parser.parse_args()
    if args.inside:
        print(json.dumps(container_checks(args.backend)))
    else:
        root = Path(__file__).resolve().parents[1]
        accept(root, args.output.resolve(), reuse_build_cache=args.reuse_build_cache)


if __name__ == "__main__":
    main()
