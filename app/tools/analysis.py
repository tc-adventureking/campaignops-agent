import math
from collections import defaultdict
from typing import Any

from app.domain.models import Analysis, AppError, ErrorCode, RootCause, ToolResult
from app.domain.semantics import DIMENSIONS, METRIC_FIELDS, aggregate, safe_ratio


def relative(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def analyze(rows: list[dict[str, Any]], expected_days: int) -> ToolResult[Analysis]:
    required = {*METRIC_FIELDS, *DIMENSIONS, "period", "observed_days"}
    if any(not required <= row.keys() for row in rows):
        raise AppError(ErrorCode.ANALYSIS, "诊断查询结果缺少必要字段")
    current_rows = [row for row in rows if row["period"] == "current"]
    previous_rows = [row for row in rows if row["period"] == "previous"]
    current, previous = aggregate(current_rows), aggregate(previous_rows)
    # An absent period has no observations; sum([]) must not become a reported zero.
    if not current_rows:
        current = dict.fromkeys(current)
    if not previous_rows:
        previous = dict.fromkeys(previous)
    changes = {
        key: {
            "absolute": (float(current[key] or 0) - float(previous[key] or 0))
            if current[key] is not None and previous[key] is not None
            else None,
            "relative": relative(current[key], previous[key]),
        }
        for key in current
    }
    enough = bool(current_rows and previous_rows)
    enough = enough and all(
        float(period["clicks"] or 0) >= 100 and float(period["conversions"] or 0) >= 20
        for period in (current, previous)
    )
    enough = enough and all(row["observed_days"] == expected_days for row in rows)
    enough = enough and {tuple(row[d] for d in DIMENSIONS) for row in current_rows} == {
        tuple(row[d] for d in DIMENSIONS) for row in previous_rows
    }
    limitations = [
        "合成数据；规则诊断仅提供候选解释，不证明因果。置信度是启发式评分，不是统计概率。"
    ]
    if not enough:
        limitations.append("本期/上期数据为空、日期不完整或样本不足，无法给出可靠根因。")
    contributions: dict[str, list[dict[str, Any]]] = {}
    for dimension in DIMENSIONS:
        grouped: dict[Any, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: {"current": [], "previous": []}
        )
        for row in rows:
            grouped[row[dimension]][row["period"]].append(row)
        details = []
        for value, periods in grouped.items():
            now, before = aggregate(periods["current"]), aggregate(periods["previous"])
            delta = float(now["conversions"] or 0) - float(before["conversions"] or 0)
            total_delta = changes["conversions"]["absolute"] or 0
            details.append(
                {
                    "value": value,
                    "conversion_delta": delta,
                    "contribution": safe_ratio(delta, total_delta),
                    "cpc_change": relative(now["cpc"], before["cpc"]),
                    "cvr_change": relative(now["cvr"], before["cvr"]),
                    "spend_delta": float(now["spend"] or 0) - float(before["spend"] or 0),
                }
            )
        contributions[dimension] = sorted(details, key=lambda x: -abs(x["conversion_delta"]))
    # Log decomposition is exact for strictly positive funnel components.
    funnel = {}
    for metric in ("impressions", "ctr", "cvr"):
        metric_now, metric_before = current[metric], previous[metric]
        funnel[metric] = (
            math.log(metric_now / metric_before) if metric_now and metric_before else 0.0
        )
    funnel_total = sum(abs(value) for value in funnel.values())
    causes: list[RootCause] = []

    def add(
        code: str, title: str, metric: str, strength: float, contribution: float | None = None
    ) -> None:
        sample = min(1.0, min(float(current["clicks"] or 0), float(previous["clicks"] or 0)) / 1000)
        signal = min(1.0, abs(strength) / 0.5)
        stable = sum(abs(changes[m]["relative"] or 0) < 0.1 for m in ("ctr", "cvr", "cpc")) / 3
        confidence = min(0.95, 0.4 * sample + 0.4 * signal + 0.2 * stable)
        weight = abs(funnel.get(metric, 0)) / funnel_total if funnel_total else 0
        causes.append(
            RootCause(
                code=code,
                title=title,
                confidence=round(confidence, 4),
                contribution=round(weight if contribution is None else contribution, 4),
                supporting_evidence=[
                    f"{metric}: 本期={current.get(metric)}, 上期={previous.get(metric)}, 相对变化={changes.get(metric, {}).get('relative')}"
                ],
                counter_evidence=[
                    f"{m} 保持稳定"
                    for m in ("ctr", "cvr", "cpc")
                    if m != metric and abs(changes[m]["relative"] or 0) < 0.1
                ],
                confidence_components={
                    "sample": sample,
                    "signal": signal,
                    "control_stability": stable,
                },
            )
        )

    if enough:
        channel_changes = [entry["cpc_change"] or 0 for entry in contributions["channel_id"]]
        channel_specific = (
            len(channel_changes) >= 2 and max(channel_changes) > 0.3 and min(channel_changes) < 0.1
        )
        if channel_specific:
            add("channel_anomaly", "单一渠道成本异常", "cpc", max(channel_changes), 1.0)
        if (
            current["budget_limited"]
            and (current["budget_utilization"] or 0) > 0.95
            and (changes["impressions"]["relative"] or 0) < -0.2
        ):
            add("budget_limited", "预算受限导致流量收缩", "impressions", -0.6)
        elif (changes["impressions"]["relative"] or 0) < -0.2:
            add(
                "traffic_drop",
                "曝光流量下降",
                "impressions",
                changes["impressions"]["relative"] or 0,
            )
        if (changes["ctr"]["relative"] or 0) < -0.2:
            add("ctr_drop", "点击率下降，需排查创意与人群", "ctr", changes["ctr"]["relative"] or 0)
        if (changes["cvr"]["relative"] or 0) < -0.2:
            add(
                "cvr_drop",
                "点击转化率下降，需排查落地页与人群",
                "cvr",
                changes["cvr"]["relative"] or 0,
            )
        if (changes["cpc"]["relative"] or 0) > 0.2:
            add("cpc_rise", "单次点击成本上涨", "cpc", changes["cpc"]["relative"] or 0, 1.0)
        causes.sort(
            key=lambda cause: (
                cause.code != "channel_anomaly",
                -cause.confidence,
                -cause.contribution,
            )
        )
    return ToolResult(
        status="ok",
        data=Analysis(
            current=current,
            previous=previous,
            changes=changes,
            root_causes=causes,
            contributions=contributions,
            sufficient_data=enough,
            limitations=limitations,
        ),
    )
