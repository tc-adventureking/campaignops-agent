"""Small live API capability probe; reads secrets from .env and never prints them."""

import argparse
import asyncio
import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from app.agent.planning import TaskContext
from app.agent.runners import OpenAICompatibleRunner, YoutuRunner
from app.domain.models import AppError
from app.settings import Settings


async def probe(settings: Settings, include_youtu: bool = True) -> dict[str, Any]:
    if not settings.model_api_key.get_secret_value():
        raise SystemExit("请在项目根目录 .env 的 MODEL_API_KEY 中填写密钥。")
    results: list[dict[str, Any]] = []
    headers = {"Authorization": "Bearer " + settings.model_api_key.get_secret_value()}
    endpoint = settings.model_base_url.rstrip("/") + "/chat/completions"
    common: dict[str, Any] = {
        "model": settings.model_name,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "Reply with exactly the number 5."}],
    }
    if "deepseek.com" in settings.model_base_url:
        common["thinking"] = {"type": "disabled"}
    async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
        for streaming in (False, True):
            label = "streaming" if streaming else "ordinary"
            started = time.perf_counter()
            try:
                if streaming:
                    content = ""
                    async with client.stream(
                        "POST", endpoint, headers=headers, json=common | {"stream": True}
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if line.startswith("data: ") and line != "data: [DONE]":
                                body = json.loads(line[6:])
                                if body.get("choices"):
                                    content += (
                                        body["choices"][0].get("delta", {}).get("content") or ""
                                    )
                else:
                    response = await client.post(endpoint, headers=headers, json=common)
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"].get("content", "")
                results.append(
                    {
                        "check": label,
                        "passed": content.strip() == "5",
                        "latency_ms": (time.perf_counter() - started) * 1000,
                    }
                )
            except Exception as exc:
                results.append({"check": label, "passed": False, "error_type": type(exc).__name__})
    context = TaskContext("query", date(2026, 8, 23), date(2026, 8, 29), date(2026, 8, 16), 3, [])
    runners = [("compatible_tool_call", OpenAICompatibleRunner(settings))]
    if include_youtu:
        runners.append(("youtu_tool_call", YoutuRunner(settings)))  # type: ignore[arg-type]
    for label, runner in runners:
        for repeat in range(1, 4):
            started = time.perf_counter()
            try:
                plan = await runner.plan("查询 Campaign 3 最近7天 CVR", context, [])
                from app.guardrails.sql import validate_sql

                validated = validate_sql(plan.sql)
                results.append(
                    {
                        "check": label,
                        "repeat": repeat,
                        "passed": True,
                        "query_hash": validated.query_hash,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "metrics": runner.metrics.model_dump(),
                    }
                )
            except AppError as exc:
                results.append(
                    {
                        "check": label,
                        "repeat": repeat,
                        "passed": False,
                        "error": exc.info.model_dump(mode="json"),
                    }
                )
                break
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "model": settings.model_name,
        "passed": all(row["passed"] for row in results),
        "checks": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--without-youtu", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/model-probe.json"))
    args = parser.parse_args()
    result = asyncio.run(probe(Settings(), not args.without_youtu))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
