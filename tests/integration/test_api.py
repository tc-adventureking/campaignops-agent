import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.settings import Settings


def wait_terminal(client: TestClient, run_id: str) -> dict:
    for _ in range(200):
        run = client.get(f"/v1/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_api_sse_resume_and_sources(settings: Settings, tmp_path: Path) -> None:
    # Each API instance owns its own state database.
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        response = client.post("/v1/runs", json={"question": "诊断 Campaign 3 CVR 下滑原因"})
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        run = wait_terminal(client, run_id)
        assert run["status"] == "succeeded", run
        first = client.get(f"/v1/runs/{run_id}/events")
        assert "event: result" in first.text
        ids = [int(line[4:]) for line in first.text.splitlines() if line.startswith("id: ")]
        resume = client.get(f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(ids[2])})
        assert f"id: {ids[2]}\n" not in resume.text
        assert f"id: {ids[3]}\n" in resume.text
        assert (
            client.get(f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": "-1"}).status_code
            == 400
        )
        assert (
            client.get(f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": "999999"}).status_code
            == 409
        )
        citation = run["answer"]["citations"][0]
        assert (
            client.get("/v1/knowledge/" + citation["chunk_id"]).json()["version"]
            == citation["version"]
        )


def test_api_validation_and_concurrent_requests(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/runs", json={"question": " "}).status_code == 422
        assert client.get("/v1/runs/missing").status_code == 404
        ids = [
            client.post("/v1/runs", json={"question": f"诊断 Campaign {cid}"}).json()["run_id"]
            for cid in (1, 2, 3)
        ]
        results = [wait_terminal(client, run_id) for run_id in ids]
        assert all(run["status"] == "succeeded" for run in results)
        assert len({run["trace_id"] for run in results}) == 3


def test_readiness_failure(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, agent_mode="demo", data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_workspace_assets_and_markdown_endpoint(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "诊断工作台" in home.text
        assert "script-src 'self'" in home.headers["content-security-policy"]
        for filename in ("workspace.css", "workspace.js", "favicon.svg"):
            assert client.get(f"/static/{filename}").status_code == 200
        workspace = client.get("/v1/workspace").json()
        assert workspace["mode"] == "demo"
        assert workspace["data"]["rows"] == 4320
        assert len(workspace["knowledge"]) == 15
        assert "model_api_key" not in str(workspace)
        rendered = client.post(
            "/v1/markdown", json={"markdown": "# Report\n\n<script>alert(1)</script>"}
        )
        assert rendered.status_code == 200
        assert "<h1>Report</h1>" in rendered.json()["html"]
        assert "<script>" not in rendered.json()["html"]
        assert client.post("/v1/markdown", json={"markdown": "x" * 100001}).status_code == 422


def test_overload_returns_429(settings: Settings) -> None:
    import asyncio

    api = create_app(settings.model_copy(update={"max_concurrent_runs": 1, "max_queued_runs": 0}))
    with TestClient(api) as client:
        original = api.state.workflow.run

        async def slow_run(*args, **kwargs):
            await asyncio.sleep(0.2)
            return await original(*args, **kwargs)

        api.state.workflow.run = slow_run
        first = client.post("/v1/runs", json={"question": "什么是 CTR"})
        assert first.status_code == 202
        assert client.post("/v1/runs", json={"question": "什么是 CVR"}).status_code == 429
        assert wait_terminal(client, first.json()["run_id"])["status"] == "succeeded"
