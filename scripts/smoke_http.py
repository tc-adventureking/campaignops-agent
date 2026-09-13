"""Start a temporary local API and verify real HTTP, concurrent runs and SSE reconnect."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx


def smoke(mode: str) -> dict[str, Any]:
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    Path("artifacts").mkdir(exist_ok=True)
    with Path("artifacts/http-smoke-server.log").open("w") as logs:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=os.environ | {"AGENT_MODE": mode},
            stdout=logs,
            stderr=logs,
        )
        try:
            with httpx.Client(base_url=base, timeout=90) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "API did not become ready; see artifacts/http-smoke-server.log"
                        )
                    time.sleep(0.1)
                assert client.get("/").status_code == 200
                assert client.get("/openapi.json").status_code == 200
                questions = [
                    "诊断 Campaign 3 最近7天 CVR 下滑的原因",
                    "诊断 Campaign 6 最近7天渠道成本异常",
                    "将 Campaign 3 预算提高20%",
                ]

                def create(question: str) -> str:
                    response = client.post("/v1/runs", json={"question": question})
                    response.raise_for_status()
                    return str(response.json()["run_id"])

                with ThreadPoolExecutor(max_workers=3) as pool:
                    ids = list(pool.map(create, questions))
                first_id = 0
                with client.stream("GET", f"/v1/runs/{ids[0]}/events") as stream:
                    for line in stream.iter_lines():
                        if line.startswith("id: "):
                            first_id = int(line[4:])
                            break  # Closing the HTTP connection must not cancel the run.
                runs = []
                for run_id in ids:
                    deadline = time.monotonic() + 90
                    while True:
                        run = client.get(f"/v1/runs/{run_id}").json()
                        if run["status"] not in ("queued", "running"):
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError("HTTP run timed out")
                        time.sleep(0.05)
                    runs.append(run)
                assert [run["status"] for run in runs] == [
                    "succeeded",
                    "succeeded",
                    "approval_required",
                ], [run.get("error") for run in runs]
                assert runs[0]["answer"]["root_causes"][0]["code"] == "cvr_drop"
                assert runs[1]["answer"]["root_causes"][0]["code"] == "channel_anomaly"
                assert not runs[2]["approval"]["executable"]
                assert len({run["trace_id"] for run in runs}) == 3
                resumed = client.get(
                    f"/v1/runs/{ids[0]}/events", headers={"Last-Event-ID": str(first_id)}
                )
                event_ids = [
                    int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")
                ]
                assert event_ids and min(event_ids) > first_id
                assert "event: result" in resumed.text
                return {
                    "passed": True,
                    "mode": mode,
                    "concurrent_runs": 3,
                    "disconnect_resume": True,
                    "statuses": [run["status"] for run in runs],
                    "trace_ids": [run["trace_id"] for run in runs],
                }
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["demo", "openai", "youtu"], default="demo")
    args = parser.parse_args()
    result = smoke(args.mode)
    Path("artifacts/http-smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
