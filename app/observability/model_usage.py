"""Retain provider cache usage before an SDK normalizes away extension fields."""

from typing import Any, TypedDict

import httpx


class CacheCounts(TypedDict):
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None


def counter(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def cache_counts(usage: dict[str, Any]) -> CacheCounts:
    hit = counter(usage.get("prompt_cache_hit_tokens"))
    miss = counter(usage.get("prompt_cache_miss_tokens"))
    if hit is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            hit = counter(details.get("cached_tokens"))
    total = counter(usage.get("prompt_tokens"))
    if miss is None and hit is not None and total is not None and hit <= total:
        miss = total - hit
    return {"cache_hit_tokens": hit, "cache_miss_tokens": miss}


class CacheUsageCollector:
    def __init__(self) -> None:
        self.records: list[CacheCounts] = []

    async def on_response(self, response: httpx.Response) -> None:
        if not response.is_success or "application/json" not in response.headers.get(
            "content-type", ""
        ):
            return
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            return
        if isinstance(body, dict) and isinstance(body.get("usage"), dict):
            self.records.append(cache_counts(body["usage"]))

    def totals(self) -> CacheCounts:
        def total(values: list[int | None]) -> int | None:
            if not values or any(value is None for value in values):
                return None
            return sum(value for value in values if value is not None)

        return {
            "cache_hit_tokens": total([row["cache_hit_tokens"] for row in self.records]),
            "cache_miss_tokens": total([row["cache_miss_tokens"] for row in self.records]),
        }
