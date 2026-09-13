"""Readable Markdown built from executed data; presentation never changes the evidence."""

import math
from typing import Any
from urllib.parse import quote

from app.domain.models import Analysis, Answer, QueryData

LABELS = {
    "impressions": "曝光量",
    "clicks": "点击量",
    "conversions": "转化量",
    "spend": "消耗（元）",
    "conversion_value": "转化价值（元）",
    "daily_budget": "预算（元）",
    "ctr": "点击率 CTR",
    "cvr": "转化率 CVR",
    "impression_cvr": "曝光转化率",
    "cpc": "点击成本 CPC（元）",
    "cpa": "转化成本 CPA（元）",
    "roas": "广告回报 ROAS",
    "budget_utilization": "预算使用率",
    "budget_limited": "受限切片数",
    "campaign_id": "Campaign",
    "channel_id": "渠道 ID",
    "region_id": "地域 ID",
    "device_id": "设备 ID",
    "creative_id": "创意 ID",
    "ad_group_id": "广告组 ID",
    "account_id": "账户 ID",
    "channel_name": "渠道",
    "campaign_name": "Campaign 名称",
    "date": "日期",
    "period": "周期",
    "observed_days": "覆盖天数",
}
PERCENTAGES = {"ctr", "cvr", "impression_cvr", "budget_utilization"}
COUNTS = {"impressions", "clicks", "conversions", "budget_limited", "observed_days"}


def cell(value: Any) -> str:
    return (
        str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    )


def display(value: Any, metric: str = "") -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if metric in PERCENTAGES:
            return f"{value:.2%}"
        if metric in COUNTS:
            return f"{value:,.0f}"
        if metric.endswith("_id"):
            return str(int(value))
        return f"{value:,.2f}"
    return cell({"current": "本期", "previous": "上期"}.get(str(value), value))


def period_value(period: dict[str, float | None], metric: str) -> float | str | None:
    if period and all(value is None for value in period.values()):
        return "无数据"
    return period.get(metric)


def diagnostic_facts(analysis: Analysis, query_hash: str) -> list[str]:
    return [
        f"{metric}: 本期={period_value(analysis.current, metric)}；上期={period_value(analysis.previous, metric)}；变化={analysis.changes[metric]} [SQL {query_hash}]"
        for metric in ("impressions", "clicks", "conversions", "spend", "ctr", "cvr", "cpc")
    ]


def render(answer: Answer, mode: str, query: QueryData | None = None) -> str:
    lines = ["# 投放诊断报告" if answer.analysis else "# 数据分析报告", "", answer.conclusion, ""]
    if mode == "demo":
        lines += ["> 离线规则演示 · 合成广告数据；本结果不代表真实模型能力。", ""]
    if answer.analysis:
        analysis = answer.analysis
        lines += [
            "## 核心指标",
            "",
            "本期与等长上期比较。相对变化以上期为基准，数值仅在展示时四舍五入。",
            "",
            "| 指标 | 本期 | 上期 | 相对变化 |",
            "| :--- | ---: | ---: | ---: |",
        ]
        for key in ("impressions", "clicks", "conversions", "spend", "ctr", "cvr", "cpc", "roas"):
            change = analysis.changes.get(key, {}).get("relative")
            delta = f"{change:+.2%}" if change is not None else "—"
            lines.append(
                f"| {LABELS[key]} | {display(period_value(analysis.current, key), key)} | {display(period_value(analysis.previous, key), key)} | {delta} |"
            )
        if answer.root_causes:
            lines += [
                "",
                "## 候选原因",
                "",
                "以下为规则诊断的候选解释，需结合业务变更与实验确认因果。",
            ]
            for index, cause in enumerate(answer.root_causes, 1):
                lines += [
                    "",
                    f"### {index}. {cause.title}",
                    "",
                    f"贡献度 **{cause.contribution:.1%}** · 规则置信度 **{cause.confidence:.0%}**",
                ]
                if cause.counter_evidence:
                    lines += [
                        "",
                        "其他稳定信号：",
                        *[f"- {item}" for item in cause.counter_evidence],
                    ]
    elif query:
        columns = query.columns
        lines += [
            "## 查询结果",
            "",
            f"查询返回 {query.row_count} 行；下表展示前 {min(20, query.row_count)} 行。",
            "",
            "| " + " | ".join(cell(LABELS.get(key, key)) for key in columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        for row in query.rows[:20]:
            lines.append("| " + " | ".join(display(row.get(key), key) for key in columns) + " |")
    elif answer.facts:
        lines += ["## 业务口径", "", *[f"- {fact}" for fact in answer.facts]]
    for title, items in (
        ("建议行动", answer.recommendations),
        ("适用范围与限制", answer.limitations),
    ):
        if items:
            lines += ["", f"## {title}", "", *[f"- {item}" for item in items]]
    if answer.evidence or answer.citations:
        lines += ["", "## 证据与口径", ""]
        for evidence in answer.evidence:
            if evidence.kind == "query":
                lines += [
                    f"- 数据查询：返回 **{evidence.row_count} 行**。查询指纹：`{evidence.query_hash}`。"
                ]
        for evidence in answer.citations:
            chunk = evidence.chunk_id or ""
            lines += [
                f"- [{cell(chunk)}](/v1/knowledge/{quote(chunk, safe='')}) · 文档版本 {cell(evidence.version)}"
            ]
    return "\n".join(lines)
