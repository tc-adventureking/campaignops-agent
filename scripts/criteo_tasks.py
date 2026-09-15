"""Broader external query suite; Python references are independent of oracle SQL."""

import hashlib
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

VERSION = "criteo-expanded-v1"


def total(rows: list[dict[str, Any]], field: str) -> float | None:
    return math.fsum(r[field] for r in rows) if rows else None


def divide(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def grouped(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    return dict(sorted(groups.items()))


def make_expanded_tasks(rows: list[dict[str, Any]], excluded: set[int]) -> list[dict[str, Any]]:
    campaigns = sorted(
        {r["campaign_id"] for r in rows} - excluded,
        key=lambda c: hashlib.sha256(f"{VERSION}:{c}".encode()).hexdigest(),
    )
    if len(campaigns) < 80:
        raise ValueError("Expanded suite requires 80 campaigns outside the original regression set")
    latest = date.fromisoformat(max(r["date"] for r in rows)) - timedelta(days=1)
    return [
        task
        for scenario in range(10)
        for task in scenario_tasks(
            rows, campaigns[scenario * 8 : (scenario + 1) * 8], scenario, latest
        )
    ]


def scenario_tasks(
    rows: list[dict[str, Any]],
    ids: list[int],
    scenario: int,
    latest: date,
    *,
    window_days: int | None = None,
    end_override: date | None = None,
    top_k: int = 3,
    min_impressions: int = 50,
    min_active_days: int = 3,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    split = "dev" if scenario < 3 else "test"
    days = window_days if window_days is not None else [3, 5, 7, 10, 4][scenario % 5]
    if not 1 <= days <= 14 or not 1 <= top_k <= 10 or min_impressions < 0 or min_active_days < 1:
        raise ValueError("Invalid scenario parameters")
    end = end_override if end_override is not None else latest - timedelta(days=scenario % 4)
    start = end - timedelta(days=days - 1)
    previous = start - timedelta(days=days)
    selected = [r for r in rows if r["campaign_id"] in ids]
    current = [r for r in selected if str(start) <= r["date"] <= str(end)]
    before = [r for r in selected if str(previous) <= r["date"] < str(start)]
    both = before + current
    scope = f"campaign_id IN ({','.join(map(str, ids))}) AND date BETWEEN DATE '{start}' AND DATE '{end}'"
    both_scope = f"campaign_id IN ({','.join(map(str, ids))}) AND date BETWEEN DATE '{previous}' AND DATE '{end}'"
    by_campaign = grouped(current, "campaign_id")
    by_day = grouped(current, "date")
    prefix = f"人工日期 {start} 至 {end}（含首尾），仅考虑 Campaign ID {','.join(map(str, ids))}："

    def add(
        family: str,
        question: str,
        sql: str,
        expected: list[dict[str, Any]],
        ordered: bool = True,
    ) -> None:
        tasks.append(
            {
                "id": f"expanded-{split}-{scenario:02d}-{family}",
                "split": split,
                "family": family,
                "campaign_id": None,
                "campaign_ids": ids,
                "start_date": str(start),
                "end_date": str(end),
                "question": prefix + question,
                "oracle_sql": sql,
                "expected_rows": expected,
                "ordered": ordered,
                "label_origin": "developer_template_independent_python_reference",
                "version": VERSION,
                "window_days": days,
            }
        )

    add(
        "multi_totals",
        "汇总 impressions、clicks、spend。",
        f"SELECT SUM(impressions) AS impressions,SUM(clicks) AS clicks,SUM(spend) AS spend FROM daily_metrics WHERE {scope}",
        [{k: total(current, k) for k in ("impressions", "clicks", "spend")}],
    )
    add(
        "weighted_ratios",
        "计算整体 ctr 与 cpc，返回原始比率；分母为零返回 NULL。",
        f"SELECT SUM(clicks)/NULLIF(SUM(impressions),0) AS ctr,SUM(spend)/NULLIF(SUM(clicks),0) AS cpc FROM daily_metrics WHERE {scope}",
        [
            {
                "ctr": divide(total(current, "clicks"), total(current, "impressions")),
                "cpc": divide(total(current, "spend"), total(current, "clicks")),
            }
        ],
    )
    add(
        "multi_daily",
        "按日期升序输出 date、clicks、spend，每日跨所有指定 Campaign 汇总。",
        f"SELECT date,SUM(clicks) AS clicks,SUM(spend) AS spend FROM daily_metrics WHERE {scope} GROUP BY date ORDER BY date",
        [
            {"date": d, "clicks": total(rs, "clicks"), "spend": total(rs, "spend")}
            for d, rs in by_day.items()
        ],
    )
    ranks = [{"campaign_id": c, "spend": total(rs, "spend")} for c, rs in by_campaign.items()]
    add(
        "top_spend",
        f"输出变换费用最高的{top_k}个 Campaign：campaign_id、spend，费用降序，同值 ID 升序。",
        f"SELECT campaign_id,SUM(spend) AS spend FROM daily_metrics WHERE {scope} GROUP BY campaign_id ORDER BY spend DESC,campaign_id LIMIT {top_k}",
        sorted(ranks, key=lambda r: (-r["spend"], r["campaign_id"]))[:top_k],
    )
    ranks = [
        {"campaign_id": c, "ctr": divide(total(rs, "clicks"), total(rs, "impressions"))}
        for c, rs in by_campaign.items()
        if (total(rs, "impressions") or 0) >= min_impressions
    ]
    add(
        "top_ctr_having",
        f"仅保留期间总曝光至少{min_impressions}的 Campaign，输出 ctr 最高{top_k}个 campaign_id、ctr，ctr 降序，同值 ID 升序。",
        f"SELECT campaign_id,SUM(clicks)/NULLIF(SUM(impressions),0) AS ctr FROM daily_metrics WHERE {scope} GROUP BY campaign_id HAVING SUM(impressions)>={min_impressions} ORDER BY ctr DESC,campaign_id LIMIT {top_k}",
        sorted(ranks, key=lambda r: (-r["ctr"], r["campaign_id"]))[:top_k],
    )
    ranks = [
        {"campaign_id": c, "cpc": divide(total(rs, "spend"), total(rs, "clicks"))}
        for c, rs in by_campaign.items()
        if (total(rs, "clicks") or 0) > 0
    ]
    add(
        "lowest_cpc",
        f"仅考虑总点击大于0的 Campaign，输出 cpc 最低{top_k}个 campaign_id、cpc，cpc 升序，同值 ID 升序。",
        f"SELECT campaign_id,SUM(spend)/NULLIF(SUM(clicks),0) AS cpc FROM daily_metrics WHERE {scope} GROUP BY campaign_id HAVING SUM(clicks)>0 ORDER BY cpc,campaign_id LIMIT {top_k}",
        sorted(ranks, key=lambda r: (r["cpc"], r["campaign_id"]))[:top_k],
    )
    add(
        "daily_distinct",
        "逐日统计有日志的不同 Campaign 数（不要求有点击），输出 date、active_campaigns，按日期升序。",
        f"SELECT date,COUNT(DISTINCT campaign_id) AS active_campaigns FROM daily_metrics WHERE {scope} GROUP BY date ORDER BY date",
        [
            {"date": d, "active_campaigns": len({r["campaign_id"] for r in rs})}
            for d, rs in by_day.items()
        ],
    )
    add(
        "active_days_having",
        f"仅保留至少{min_active_days}个日志日期的 Campaign，输出 campaign_id、active_days，按 ID 升序。",
        f"SELECT campaign_id,COUNT(DISTINCT date) AS active_days FROM daily_metrics WHERE {scope} GROUP BY campaign_id HAVING COUNT(DISTINCT date)>={min_active_days} ORDER BY campaign_id",
        [
            {"campaign_id": c, "active_days": len({r["date"] for r in rs})}
            for c, rs in by_campaign.items()
            if len({r["date"] for r in rs}) >= min_active_days
        ],
    )
    add(
        "zero_click_counts",
        "每个有日志的 Campaign 有多少条零点击日记录？即使为0也保留，输出 campaign_id、zero_click_days，按 ID 升序。",
        f"SELECT campaign_id,SUM(CASE WHEN clicks=0 THEN 1 ELSE 0 END) AS zero_click_days FROM daily_metrics WHERE {scope} GROUP BY campaign_id ORDER BY campaign_id",
        [
            {"campaign_id": c, "zero_click_days": sum(r["clicks"] == 0 for r in rs)}
            for c, rs in by_campaign.items()
        ],
    )
    buckets = grouped(
        [{**r, "bucket": "zero" if r["clicks"] == 0 else "positive"} for r in current], "bucket"
    )
    add(
        "conditional_buckets",
        "按每条日记录点击是否为0分为 'zero' 和 'positive'，统计各组记录数 records，输出 bucket、records，按 bucket 升序；没有记录的组不补行。",
        f"SELECT CASE WHEN clicks=0 THEN 'zero' ELSE 'positive' END AS bucket,COUNT(*) AS records FROM daily_metrics WHERE {scope} GROUP BY bucket ORDER BY bucket",
        [{"bucket": b, "records": len(rs)} for b, rs in buckets.items()],
    )
    for metric in ("spend_delta", "click_growth", "ctr_delta", "cpc_delta"):
        out = []
        for c, rs in grouped(both, "campaign_id").items():
            now = [r for r in rs if r["date"] >= str(start)]
            old = [r for r in rs if r["date"] < str(start)]

            def sums(part: list[dict[str, Any]], key: str) -> float:
                return total(part, key) or 0.0

            value: float | None
            if metric == "spend_delta":
                value = sums(now, "spend") - sums(old, "spend")
            elif metric == "click_growth":
                value = divide(sums(now, "clicks") - sums(old, "clicks"), sums(old, "clicks"))
            else:
                num, den = (
                    ("clicks", "impressions") if metric == "ctr_delta" else ("spend", "clicks")
                )
                a, b = (
                    divide(sums(now, num), sums(now, den)),
                    divide(sums(old, num), sums(old, den)),
                )
                value = (
                    (a - b) * (100 if metric == "ctr_delta" else 1)
                    if a is not None and b is not None
                    else None
                )
            out.append({"campaign_id": c, metric: value})

        def agg(field: str, current_period: bool) -> str:
            return f"SUM(CASE WHEN date {'>=' if current_period else '<'} DATE '{start}' THEN {field} ELSE 0 END)"

        if metric == "spend_delta":
            expr = f"{agg('spend', True)}-{agg('spend', False)}"
            desc = "本期费用减去上期费用"
        elif metric == "click_growth":
            expr = (
                f"({agg('clicks', True)}-{agg('clicks', False)})/NULLIF({agg('clicks', False)},0)"
            )
            desc = "点击相对增长率（本期减上期）/上期，返回小数"
        else:
            num, den = ("clicks", "impressions") if metric == "ctr_delta" else ("spend", "clicks")
            expr = f"({agg(num, True)}/NULLIF({agg(den, True)},0)-{agg(num, False)}/NULLIF({agg(den, False)},0))*{100 if metric == 'ctr_delta' else 1}"
            desc = (
                "本期 CTR 减上期 CTR 的百分点差"
                if metric == "ctr_delta"
                else "本期 CPC 减上期 CPC 的绝对差"
            )
        add(
            metric,
            f"与上期 {previous} 至 {start - timedelta(days=1)} 比较，按 Campaign 计算{desc}；任一需要的分母为0返回 NULL。保留两期任一期有日志的 Campaign，缺期可加总量按0，输出 campaign_id、{metric}，按 ID 升序。",
            f"SELECT campaign_id,{expr} AS {metric} FROM daily_metrics WHERE {both_scope} GROUP BY campaign_id ORDER BY campaign_id",
            out,
        )
    add(
        "spend_share",
        "每个有日志的 Campaign 的费用占指定 Campaign 集合总费用的比重，小数输出 campaign_id、spend_share，按 ID 升序。",
        f"SELECT campaign_id,SUM(spend)/NULLIF((SELECT SUM(spend) FROM daily_metrics WHERE {scope}),0) AS spend_share FROM daily_metrics WHERE {scope} GROUP BY campaign_id ORDER BY campaign_id",
        [
            {
                "campaign_id": c,
                "spend_share": divide(total(rs, "spend"), total(current, "spend")),
            }
            for c, rs in by_campaign.items()
        ],
    )
    weeks = grouped(
        [
            {
                **r,
                "week_start": str(
                    date.fromisoformat(r["date"])
                    - timedelta(days=date.fromisoformat(r["date"]).weekday())
                ),
            }
            for r in current
        ],
        "week_start",
    )
    add(
        "weekly_totals",
        "以星期一为周首汇总指定日期范围内的点击，输出 DATE 类型 week_start、clicks，按周首升序；不要把范围外日期加入。",
        f"SELECT CAST(DATE_TRUNC('week',date) AS DATE) AS week_start,SUM(clicks) AS clicks FROM daily_metrics WHERE {scope} GROUP BY week_start ORDER BY week_start",
        [{"week_start": w, "clicks": total(rs, "clicks")} for w, rs in weeks.items()],
    )
    add(
        "distinct_positive",
        "列出期间至少有一天点击大于0的不同 campaign_id，去重并按 ID 升序。",
        f"SELECT DISTINCT campaign_id FROM daily_metrics WHERE {scope} AND clicks>0 ORDER BY campaign_id",
        [
            {"campaign_id": c}
            for c in sorted({r["campaign_id"] for r in current if r["clicks"] > 0})
        ],
    )
    add(
        "date_coverage",
        "每个有日志的 Campaign 的首末日志日期，输出 campaign_id、first_date、last_date，按 ID 升序。",
        f"SELECT campaign_id,MIN(date) AS first_date,MAX(date) AS last_date FROM daily_metrics WHERE {scope} GROUP BY campaign_id ORDER BY campaign_id",
        [
            {
                "campaign_id": c,
                "first_date": min(r["date"] for r in rs),
                "last_date": max(r["date"] for r in rs),
            }
            for c, rs in by_campaign.items()
        ],
    )
    add(
        "empty_aggregate",
        "仅选曝光数为负的记录，仍返回单行 SUM 聚合 impressions、clicks；空集合保持 SQL SUM 的 NULL 语义，不填0。",
        f"SELECT SUM(impressions) AS impressions,SUM(clicks) AS clicks FROM daily_metrics WHERE {scope} AND impressions<0",
        [{"impressions": None, "clicks": None}],
    )
    zero = [r for r in current if r["clicks"] == 0]
    add(
        "zero_denominator",
        "仅选零点击记录，计算这些记录合计费用除以合计点击数 cpc；零分母或空集合返回单行 NULL。",
        f"SELECT SUM(spend)/NULLIF(SUM(clicks),0) AS cpc FROM daily_metrics WHERE {scope} AND clicks=0",
        [{"cpc": divide(total(zero, "spend"), total(zero, "clicks"))}],
    )
    return tasks
