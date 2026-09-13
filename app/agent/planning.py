import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from app.domain.models import AppError, ErrorCode, RunRequest, SQLPlan
from app.domain.semantics import DIMENSIONS, METRIC_FIELDS, RATIOS
from app.guardrails.injection import suspicious

Intent = Literal["definition", "query", "diagnosis", "approval", "refusal", "insufficient"]


@dataclass(frozen=True)
class TaskContext:
    intent: Intent
    start: date
    end: date
    previous_start: date
    campaign_id: int | None
    assumptions: list[str]

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def classify(question: str) -> Intent:
    if suspicious(question):
        return "refusal"
    if re.search(
        r"忽略.*(?:规则|指令)|绕过|系统提示|api[_ -]?key|密钥|读取.*文件|/etc/|drop\s+table|delete\s+from|泄露|ignore.*instructions",
        question,
        re.I,
    ):
        return "refusal"
    if re.search(
        r"(?:修改|提高|提升|增加|降低|减少|调整|设置|翻倍|改成|改为).*(?:预算|出价)|(?:预算|出价).*(?:修改|提高|提升|增加|降低|减少|调整|翻倍|改成|改为)|暂停.*(?:投放|广告)|(?:increase|change|set|double).*budget",
        question,
        re.I,
    ):
        return "approval"
    if re.search(r"同比|去年|年同比", question):
        return "insufficient"
    if re.search(
        r"口径|定义|怎么算|怎么计算|公式|什么是|如何计算|含义|计算方式|归因窗口|预算规则|环比规则|聚合规则",
        question,
    ):
        return "definition"
    if re.search(r"原因|诊断|异常|为什么|为何|下降|下滑|上涨|受限|根因", question):
        return "diagnosis"
    return "query"


def task_context(request: RunRequest, data_dir: Path) -> TaskContext:
    try:
        manifest = json.loads((data_dir / "manifest.json").read_text())
        end = request.end_date or date.fromisoformat(manifest["end_date"])
    except (OSError, ValueError, KeyError):
        raise AppError(ErrorCode.EXECUTION, "数据未初始化，请先运行 seed_data") from None
    start = request.start_date or end - timedelta(days=6)
    assumptions = []
    if not request.start_date:
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", request.question)
        if dates:
            try:
                start = date.fromisoformat(dates[0])
                end = date.fromisoformat(dates[-1])
            except ValueError:
                raise AppError(ErrorCode.INPUT, "日期格式无效") from None
        else:
            recent = re.search(r"(?:最近|近)(\d+)天", request.question)
            days = int(recent.group(1)) if recent else 7
            if not 1 <= days <= 91:
                raise AppError(ErrorCode.INPUT, "查询窗口必须为 1–91 天")
            start = end - timedelta(days=days - 1)
            assumptions.append(
                f"未给定绝对日期，按数据最新日期回溯：{start} 至 {end}（含首尾日）。"
            )
    if start > end or (end - start).days > 90:
        raise AppError(ErrorCode.INPUT, "日期区间无效或超过 91 天")
    campaign_id = request.campaign_id
    if campaign_id is None:
        match = re.search(r"(?:campaign|计划|活动)\s*#?\s*([1-6])\b", request.question, re.I)
        campaign_id = int(match.group(1)) if match else None
    previous_start = start - timedelta(days=(end - start).days + 1)
    assumptions.append(
        f"对照期为 {previous_start} 至 {start - timedelta(days=1)}；币种 CNY，时区 Asia/Shanghai。"
    )
    return TaskContext(
        classify(request.question), start, end, previous_start, campaign_id, assumptions
    )


def diagnostic_sql(context: TaskContext) -> str:
    fields = ", ".join(f"SUM({field}) AS {field}" for field in METRIC_FIELDS)
    dimensions = ", ".join(DIMENSIONS)
    scope = f" AND campaign_id = {context.campaign_id}" if context.campaign_id else ""
    return f"""SELECT CASE WHEN date >= DATE '{context.start}' THEN 'current' ELSE 'previous' END AS period,
        {dimensions}, COUNT(DISTINCT date) AS observed_days, {fields}
        FROM daily_metrics WHERE date BETWEEN DATE '{context.previous_start}' AND DATE '{context.end}'{scope}
        GROUP BY period, {dimensions} ORDER BY period, {dimensions}"""


def demo_plan(question: str, context: TaskContext) -> SQLPlan:
    if context.intent == "diagnosis":
        return SQLPlan(
            sql=diagnostic_sql(context),
            metrics=list(RATIOS),
            start_date=context.start,
            end_date=context.end,
            dimensions=list(DIMENSIONS),
            assumptions=context.assumptions,
        )
    lower = question.lower()
    metrics = [key for key in RATIOS if re.search(rf"\b{key}\b", lower)]
    aliases = {
        "点击率": "ctr",
        "转化率": "cvr",
        "点击成本": "cpc",
        "转化成本": "cpa",
        "广告回报": "roas",
    }
    metrics += [value for key, value in aliases.items() if key in question and value not in metrics]
    if "曝光转化率" in question:
        metrics = ["impression_cvr"]
    expressions = [
        f"SUM({num}) / NULLIF(SUM({den}), 0) AS {metric}"
        for metric in metrics
        for num, den in [RATIOS[metric]]
    ]
    if not expressions:
        expressions = [f"SUM({field}) AS {field}" for field in METRIC_FIELDS[:5]]
    dimensions: list[str] = []
    comparing = bool(re.search(r"本期.*上期|上期.*本期|环比|对比", question))
    if comparing:
        dimensions.append("period")
    if re.search(r"趋势|每天|按日|逐日", question):
        dimensions.append("date")
    for name, field in [
        ("渠道", "channel_id"),
        ("地域", "region_id"),
        ("地区", "region_id"),
        ("设备", "device_id"),
        ("Campaign", "campaign_id"),
        ("广告计划", "campaign_id"),
    ]:
        if name.lower() in lower and not (field == "campaign_id" and context.campaign_id):
            if field not in dimensions:
                dimensions.append(field)
    scope = f" AND campaign_id = {context.campaign_id}" if context.campaign_id else ""
    group = " GROUP BY " + ", ".join(dimensions) if dimensions else ""
    order = " ORDER BY " + ", ".join(dimensions) if dimensions else ""
    limit = ""
    if re.search(r"top|排名|最高|最多", lower):
        if not dimensions:
            dimensions = ["campaign_id"]
            group = " GROUP BY campaign_id"
        order = " ORDER BY " + (metrics[0] if metrics else "spend") + " DESC"
        match = re.search(r"top\s*(\d+)", lower)
        limit = f" LIMIT {min(int(match.group(1)), 20) if match else 3}"
    selections = [
        f"CASE WHEN date >= DATE '{context.start}' THEN 'current' ELSE 'previous' END AS period"
        if d == "period"
        else d
        for d in dimensions
    ]
    sql = f"SELECT {', '.join(selections + expressions)} FROM daily_metrics WHERE date BETWEEN DATE '{context.previous_start if comparing else context.start}' AND DATE '{context.end}'{scope}{group}{order}{limit}"
    return SQLPlan(
        sql=sql,
        metrics=metrics or list(METRIC_FIELDS[:5]),
        start_date=context.start,
        end_date=context.end,
        dimensions=dimensions,
        assumptions=context.assumptions,
    )


def model_context(question: str, context: TaskContext, chunks: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "intent": context.intent,
            "start_date": str(context.start),
            "end_date": str(context.end),
            "previous_start_date": str(context.previous_start),
            "previous_end_date": str(context.start - timedelta(days=1)),
            "campaign_id": context.campaign_id,
            "diagnostic_query_template": diagnostic_sql(context)
            if context.intent == "diagnosis"
            else None,
            "retrieved_untrusted": chunks,
            "question_untrusted": question,
        },
        ensure_ascii=False,
    )
