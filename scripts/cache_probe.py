"""Measure cache reuse across different questions sharing the same system prefix."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.planning import task_context
from app.agent.prompts import prompt_hash, system_prompt
from app.agent.runners import build_runner
from app.domain.models import AppError, RunRequest
from app.settings import Settings
from app.tools.sql import SQLExecutor


async def probe(settings: Settings) -> dict[str, Any]:
    if not settings.model_api_key.get_secret_value():
        raise SystemExit("请在 .env 配置 MODEL_API_KEY。")
    results: list[dict[str, Any]] = []
    for index, metric in enumerate(("CVR", "CPC", "CPA", "ROAS")):
        if index:
            await asyncio.sleep(2)
        request = RunRequest(question=f"查询 Campaign 3 最近7天 {metric}")
        context = task_context(request, settings.data_dir)
        runner = build_runner(settings)
        try:
            plan = await runner.plan(request.question, context, [])
            query = await asyncio.to_thread(SQLExecutor(settings).execute, plan.sql)
            assert query.data is not None
            usage = runner.metrics
            results.append(
                {
                    "question": request.question,
                    "passed": True,
                    "query_hash": query.data.query_hash,
                    "metrics": usage.model_dump(),
                    "cache_hit_ratio": usage.cache_hit_tokens / usage.input_tokens
                    if usage.cache_hit_tokens is not None and usage.input_tokens
                    else None,
                }
            )
        except AppError as error:
            results.append(
                {
                    "question": request.question,
                    "passed": False,
                    "error": error.info.model_dump(mode="json"),
                }
            )
            break
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": settings.agent_mode,
        "model": settings.model_name,
        "prompt_hash": prompt_hash(),
        "system_characters": len(system_prompt()),
        "passed": len(results) == 4 and all(row["passed"] for row in results),
        "cache_observed": any(
            (row.get("metrics", {}).get("cache_hit_tokens") or 0) > 0 for row in results
        ),
        "note": "四个不同问题共享同一系统前缀；命中率来自提供方 usage，不保证后续请求命中。",
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["openai", "youtu"], default="youtu")
    parser.add_argument("--output", type=Path, default=Path("artifacts/cache-probe.json"))
    args = parser.parse_args()
    report = asyncio.run(probe(Settings(agent_mode=args.mode)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
