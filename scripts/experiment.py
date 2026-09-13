"""Paired prompt ablation with frozen inputs and all outcomes retained."""

import argparse
import asyncio
import json
import statistics
from pathlib import Path
from typing import Any

from app.settings import Settings
from scripts.evaluate import evaluate

METRICS = (
    "task_success",
    "sql_execution_correct",
    "root_cause_score",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "p95_latency_ms",
)


async def experiment(settings: Settings, tasks: Path, output: Path, repeats: int) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {"plain": [], "semantic": []}
    for repeat in range(repeats):
        # Alternate ordering to reduce warm-cache/order bias; retain every raw report.
        variants = ("plain", "semantic") if repeat % 2 == 0 else ("semantic", "plain")
        for variant in variants:
            report = await evaluate(
                settings.model_copy(update={"prompt_variant": variant}),
                tasks,
                output / f"{variant}-{repeat + 1}",
            )
            groups[variant].append(report)
    frozen_keys = (
        "data_hash",
        "eval_hash",
        "knowledge_version",
        "model",
        "configuration",
        "source_hash",
        "dependency_lock_hash",
        "database_backend",
    )
    metadata = groups["plain"][0]["metadata"]
    if any(
        report["metadata"][key] != metadata[key]
        for reports in groups.values()
        for report in reports
        for key in frozen_keys
    ):
        raise ValueError("Experiment conditions changed; results cannot be paired")
    result: dict[str, Any] = {
        "repeats": repeats,
        "conditions": {key: metadata[key] for key in frozen_keys},
        "real_model": settings.agent_mode != "demo",
        "groups": {},
        "differences": {},
        "limitations": [
            "Only the system prompt differs; both arms share retrieved context, SQL guards and deterministic diagnosis.",
            "Single-host synthetic workload; no claim about real advertising outcomes.",
            "Provider prompt-cache warming and run order can affect latency.",
        ],
    }
    if repeats == 1:
        result["limitations"].append(
            "One paired run; dispersion and statistical significance cannot be estimated."
        )
    if settings.agent_mode == "demo":
        result["limitations"].append(
            "Demo ignores prompts; this validates experiment plumbing only."
        )
    for variant, reports in groups.items():
        result["groups"][variant] = {}
        for metric in METRICS:
            values = [report["metrics"][metric] for report in reports]
            known = all(value is not None for value in values)
            result["groups"][variant][metric] = {
                "mean": statistics.mean(values) if known else None,
                "stdev": statistics.stdev(values) if known and len(values) > 1 else None,
            }
        result["groups"][variant]["bad_cases"] = [
            [row["id"] for row in report["bad_cases"]] for report in reports
        ]
        result["groups"][variant]["experiment_ids"] = [
            report["metadata"]["experiment_id"] for report in reports
        ]
    for metric in METRICS:
        before, after = (
            result["groups"][variant][metric]["mean"] for variant in ("plain", "semantic")
        )
        result["differences"][metric] = {
            "absolute": after - before if before is not None and after is not None else None,
            "relative": (after - before) / before
            if before not in (None, 0) and after is not None
            else None,
        }
    (output / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    lines = [
        "# System prompt ablation",
        "",
        f"Real model: {result['real_model']}; paired repetitions: {repeats}.",
        "",
        "| Metric | Plain | Semantic | Absolute difference |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {metric} | {result['groups']['plain'][metric]['mean']} | {result['groups']['semantic'][metric]['mean']} | {result['differences'][metric]['absolute']} |"
        for metric in METRICS
    ]
    lines += ["", *[f"- {item}" for item in result["limitations"]]]
    (output / "comparison.md").write_text("\n".join(lines) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["demo", "openai", "youtu"], default="demo")
    parser.add_argument("--tasks", type=Path, default=Path("data/eval/regression.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/experiment"))
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    result = asyncio.run(
        experiment(Settings(agent_mode=args.mode), args.tasks, args.output, args.repeats)
    )
    print(json.dumps(result["differences"], indent=2))


if __name__ == "__main__":
    main()
