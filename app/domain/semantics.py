import json
from pathlib import Path
from typing import Any

METRIC_FIELDS = (
    "impressions",
    "clicks",
    "spend",
    "conversions",
    "conversion_value",
    "daily_budget",
    "budget_limited",
)
RATIOS = {
    "ctr": ("clicks", "impressions"),
    "cvr": ("conversions", "clicks"),
    "impression_cvr": ("conversions", "impressions"),
    "cpc": ("spend", "clicks"),
    "cpa": ("spend", "conversions"),
    "roas": ("conversion_value", "spend"),
    "budget_utilization": ("spend", "daily_budget"),
}
DIMENSIONS = ("campaign_id", "channel_id", "region_id", "device_id")
SCHEMA: dict[str, dict[str, str]] = {
    "accounts": {
        "account_id": "INT",
        "account_name": "VARCHAR",
        "currency": "VARCHAR",
        "timezone": "VARCHAR",
    },
    "campaigns": {"campaign_id": "INT", "account_id": "INT", "campaign_name": "VARCHAR"},
    "ad_groups": {"ad_group_id": "INT", "campaign_id": "INT", "ad_group_name": "VARCHAR"},
    "creatives": {"creative_id": "INT", "ad_group_id": "INT", "creative_name": "VARCHAR"},
    "channels": {"channel_id": "INT", "channel_name": "VARCHAR"},
    "regions": {"region_id": "INT", "region_name": "VARCHAR"},
    "devices": {"device_id": "INT", "device_name": "VARCHAR"},
    "daily_metrics": {
        "date": "DATE",
        "account_id": "INT",
        "campaign_id": "INT",
        "ad_group_id": "INT",
        "creative_id": "INT",
        "channel_id": "INT",
        "region_id": "INT",
        "device_id": "INT",
        **{field: "DOUBLE" for field in METRIC_FIELDS},
    },
}


def safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def aggregate(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        field: sum(float(row.get(field) or 0) for row in rows) for field in METRIC_FIELDS
    }
    for metric, (num, den) in RATIOS.items():
        result[metric] = safe_ratio(float(result[num] or 0), float(result[den] or 0))
    return result


def metric_catalog() -> dict[str, Any]:
    return dict(json.loads(Path("configs/metrics.json").read_text(encoding="utf-8")))
