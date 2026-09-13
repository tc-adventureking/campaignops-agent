"""Bounded HTTP+SSE or SQL concurrency benchmark. Does not call a model by default."""

import argparse
import asyncio
import json
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.observability.reporting import percentiles
from app.settings import Settings
from app.tools.sql import SQLExecutor


async def benchmark(
    settings: Settings, target: str, url: str, concurrency: int, requests: int
) -> dict[str, Any]:
    slots = asyncio.Semaphore(concurrency)
    rows: list[dict[str, Any]] = []
    executor = SQLExecutor(settings)
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        if target == "sse":
            workspace = (await client.get("/v1/workspace")).json()
            if workspace["mode"] != "demo":
                raise ValueError("SSE benchmark requires a separate demo server")

        async def one(index: int) -> None:
            async with slots:
                started = time.perf_counter()
                status, events = "ok", 0
                try:
                    if target == "sql":
                        await asyncio.to_thread(
                            executor.execute,
                            "SELECT campaign_id,SUM(spend)/NULLIF(SUM(clicks),0) AS cpc FROM daily_metrics GROUP BY campaign_id",
                        )
                    else:
                        response = await client.post(
                            "/v1/runs", json={"question": f"诊断 Campaign {index % 6 + 1}"}
                        )
                        if response.status_code == 429:
                            status = "overloaded"
                        else:
                            response.raise_for_status()
                            run_id = response.json()["run_id"]
                            async with client.stream("GET", f"/v1/runs/{run_id}/events") as stream:
                                stream.raise_for_status()
                                async for line in stream.aiter_lines():
                                    events += int(line.startswith("id:"))
                            final = (await client.get(f"/v1/runs/{run_id}")).json()
                            status = "ok" if final["status"] == "succeeded" else final["status"]
                except Exception as exc:
                    status = type(exc).__name__
                rows.append(
                    {
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "status": status,
                        "sse_events": events,
                    }
                )

        started = time.perf_counter()
        await asyncio.gather(*(one(index) for index in range(requests)))
        elapsed = time.perf_counter() - started
    executor.cache.close()
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "target": target,
        "concurrency": concurrency,
        "requests": requests,
        "database": settings.database_backend,
        "model": "rule-demo" if target == "sse" else "none",
        "hardware": {
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "cpu": platform.processor(),
        },
        "dataset_rows": json.loads((settings.data_dir / "manifest.json").read_text())["rows"],
        "elapsed_seconds": elapsed,
        "successful_requests_per_second": sum(row["status"] == "ok" for row in rows) / elapsed,
        **percentiles([row["latency_ms"] for row in rows]),
        "results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["sql", "sse"], default="sql")
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--concurrency", type=int, choices=range(1, 33), default=4)
    parser.add_argument("--requests", type=int, choices=range(1, 201), default=24)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmark.json"))
    args = parser.parse_args()
    result = asyncio.run(
        benchmark(
            Settings(agent_mode="demo"), args.target, args.url, args.concurrency, args.requests
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in result.items() if key != "results"}, indent=2))


if __name__ == "__main__":
    main()
