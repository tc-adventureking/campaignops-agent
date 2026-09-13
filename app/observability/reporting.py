import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.domain.models import Metrics
from app.observability.store import RunStore


def price(usage: Metrics, model: str, path: Path) -> Metrics:
    if model == "rule-demo":
        return usage.model_copy(
            update={"estimated_cost": 0.0, "price_version": "offline-v1", "cost_currency": "USD"}
        )
    try:
        catalog = json.loads(path.read_text())
        rate = catalog["per_million_tokens"].get(model)
        usage.price_version = catalog["version"]
        usage.cost_currency = catalog["currency"]
        if not rate or not usage.usage_complete:
            return usage
        rates = [float(rate[name]) for name in ("input", "cached_input", "output")]
        if any(not math.isfinite(value) or value < 0 for value in rates):
            return usage
        hit = usage.cache_hit_tokens
        if hit is None and rates[0] != rates[1]:
            return usage
        hit = min(hit or 0, usage.input_tokens)
        usage.estimated_cost = (
            (usage.input_tokens - hit) * rates[0] + hit * rates[1] + usage.output_tokens * rates[2]
        ) / 1_000_000
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return usage


def percentiles(values: list[float]) -> dict[str, float | None]:
    return {
        "p50_ms": statistics.median(values) if values else None,
        "p95_ms": sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None,
    }


def report(store: RunStore, limit: int = 200) -> dict[str, Any]:
    runs = store.list_runs(limit)
    stages: dict[str, list[float]] = defaultdict(list)
    errors: Counter[str] = Counter()
    retries = fallbacks = safety = 0
    for run in runs:
        events = store.events(run.run_id)
        for event in events:
            if event.event == "tool" and event.data.get("status") == "ok":
                stages[event.data["name"]].append(event.data["duration_ms"])
            if event.event == "model_attempt":
                stages["model"].append(event.data["duration_ms"])
        retries += int(any(event.event == "retry" for event in events))
        fallbacks += int(any(event.event == "fallback" for event in events))
        safety += int(
            any(
                event.event == "context" and event.data.get("intent") == "refusal"
                for event in events
            )
        )
        if run.error:
            errors[run.error.code] += 1
    count = len(runs)
    return {
        "sample_size": count,
        "sample": f"latest_{limit}_runs",
        "status_counts": dict(Counter(run.status for run in runs)),
        "success_rate": sum(run.status == "succeeded" for run in runs) / count if count else None,
        "retry_rate": retries / count if count else None,
        "fallback_rate": fallbacks / count if count else None,
        "safety_refusal_rate": safety / count if count else None,
        "failure_classes": dict(errors),
        "latency": percentiles(
            [
                run.metrics.duration_ms
                for run in runs
                if run.status in {"succeeded", "failed", "cancelled"}
            ]
        ),
        "stages": {name: percentiles(values) for name, values in stages.items()},
        "input_tokens": sum(run.metrics.input_tokens for run in runs),
        "output_tokens": sum(run.metrics.output_tokens for run in runs),
        "known_estimated_cost": sum(run.metrics.estimated_cost or 0 for run in runs),
        "unpriced_runs": sum(run.metrics.estimated_cost is None for run in runs),
        "runs": [run.model_dump(mode="json", exclude={"answer", "approval"}) for run in runs],
    }
