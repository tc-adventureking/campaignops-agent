"""Criteo query-component benchmark: independent oracles, protected SQL, and inspectable reports."""

import argparse
import asyncio
import csv
import hashlib
import html
import json
import math
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.agent.planning import TaskContext
from app.agent.runners import OpenAICompatibleRunner, YoutuRunner
from app.domain.models import AppError
from app.guardrails.sql import ALLOWED_FUNCTIONS
from app.settings import Settings
from app.tools.sql import SQLExecutor
from scripts.evaluate import same_results
from scripts.prepare_criteo import SCHEMA, sha256, write_json

VERSION = "criteo-query-v1"


def instructions(variant: str = "semantic") -> str:
    base = """你是 Criteo 广告查询规划器。只调用 submit_sql 一次，提交 DuckDB 只读 SELECT。
按问题要求使用准确的列别名和排序。SQLPlan 的起止日期必须与请求上下文一致。
结果契约：最终 SELECT 必须直接输出问题要求的每个指标及其原名别名；
只返回计算原料、在 metrics 或 assumptions 中列出指标，不能替代实际结果列。
例如用户请求 ctr、cpc，就应在最终 SELECT 中计算并命名为 ctr、cpc，不能改为 cpc_cur 等其他名字。
上下文 previous_start_date/previous_end_date 只是可用的辅助日期，不代表用户要求上期查询。
只有问题的具体计算要求明确提出上期、变化、增长或两期比较时才查询上期。
“明确指定的上期除外”等范围规则不是比较请求；不得自行增加上期指标或对比假设。
查询范围契约：问题开头给出的日期区间作用于整个问题的所有指标和子查询。
除明确要求对比的上期外，SQL 必须过滤到本期 start_date 至 end_date（含首尾日）。
MIN(date)、MAX(date)、日志首日/末日、活跃日期、去重数和覆盖范围也必须先按请求日期过滤再计算；
“首末日志日期”不表示全历史首末日期。只有用户明确要求全历史时才可扩大范围。
不得在 assumptions 中自行增加“不受请求区间限制”等假设。SQLPlan 日期正确不能替代 SQL 的实际日期过滤。
数据库仅有下面列出的表和字段，禁止访问文件、外部表或其他字段。
执行器的 SQL 语法约束：只允许单条 SELECT 或非递归 CTE；CTE 名称不得覆盖实体表名。
禁止 SELECT *（COUNT(*) 除外）、UNION/INTERSECT/EXCEPT、窗口函数、UNNEST、递归及参数占位符。
UNION ALL 同样禁止，包括在 CTE 中用它拼接用户给出的 ID 列表。
条件计数且要求保留零计数组时，直接在请求日期和 ID 范围内 GROUP BY，
使用 SUM(CASE WHEN 目标条件 THEN 1 ELSE 0 END)；不在 WHERE 中过滤掉非目标记录。
“每个有日志的分组，即使计数为0也保留”不要求构造 ID 表、补造无日志分组或额外连接。
占比的总体分母使用标量聚合子查询，该子查询与外层查询使用相同的日期和 ID 范围。
例如分组金额占整体金额：SUM(金额)/NULLIF((SELECT SUM(金额) FROM 同一表 WHERE 同一范围),0)。
不能使用 SUM(SUM(...)) OVER () 或任何 OVER 子句；总量计算也不豁免窗口函数禁令。
禁止 CROSS JOIN、逗号连接（例如 FROM a, b）及缺少 ON 条件的 JOIN；两个 CTE 各返回一行也不例外。
不要用 ON TRUE 伪装关联来绕过限制。周期对比优先在一次事实表扫描中使用条件聚合：
SUM(CASE WHEN 日期属于目标期间 THEN 指标字段 ELSE 0 END)，分别计算各期间所需的分子和分母。
跨期 SQL 的过滤区间应覆盖本期和上期，但 SQLPlan 起止日期仍填写上下文中的本期区间。
提交前检查查询符合上述约束，再调用 submit_sql；不要依赖执行失败后的重试。
数值输出契约：用户未明确指定小数位时，SQL 返回完整计算精度。
中间值和最终结果都不得使用 ROUND、截断、定点小数 CAST 或字符串格式化来减少精度。
百分比或百分点换算只做所需乘法，不对最终差值四舍五入；展示格式由结果展示层处理。
日期是从 2000-01-01 开始的人工映射日，不是真实日历日期；费用是变换后的数值，不是人民币或销售收入。
不支持转化、收入、ROAS、预算、渠道、设备、地域和根因诊断。不要编造这些字段。
"""
    if variant == "semantic":
        base += "CTR=SUM(clicks)/NULLIF(SUM(impressions),0)；CPC=SUM(spend)/NULLIF(SUM(clicks),0)。先聚合再计算比率。CTR 差值用百分点时乘以100。费用和比率不要提前四舍五入。\n"
    return base + json.dumps(
        {"schema": SCHEMA, "allowed_functions": sorted(ALLOWED_FUNCTIONS)}, ensure_ascii=False
    )


def reference_rows(root: Path) -> list[dict[str, Any]]:
    with (root / "reference.csv").open(encoding="utf-8") as handle:
        return [
            {
                "date": r["date"],
                "campaign_id": int(r["campaign_id"]),
                "impressions": int(r["impressions"]),
                "clicks": int(r["clicks"]),
                "spend": float(r["spend"]),
            }
            for r in csv.DictReader(handle)
        ]


def ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def make_tasks(root: Path) -> list[dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text())
    rows = reference_rows(root)
    # Avoid the first and last potentially incomplete relative days.
    start = date.fromisoformat(manifest["start_date"]) + timedelta(days=1)
    end = date.fromisoformat(manifest["end_date"]) - timedelta(days=1)
    if (end - start).days < 14:
        raise ValueError("Need at least 15 complete relative days")
    days: dict[int, set[str]] = {}
    for row in rows:
        if str(start) <= row["date"] <= str(end):
            days.setdefault(row["campaign_id"], set()).add(row["date"])
    candidates = [c for c, ds in days.items() if len(ds) == (end - start).days + 1]
    campaigns = sorted(
        candidates, key=lambda c: hashlib.sha256(f"{VERSION}:{c}".encode()).hexdigest()
    )[:10]
    if len(campaigns) < 10:
        raise ValueError("Need 10 campaigns with complete daily coverage")
    tasks: list[dict[str, Any]] = []
    for index, campaign in enumerate(campaigns):
        split = "dev" if index < 5 else "test"
        current_start = end - timedelta(days=6)
        previous_start = current_start - timedelta(days=7)
        current = [
            r
            for r in rows
            if r["campaign_id"] == campaign and str(current_start) <= r["date"] <= str(end)
        ]
        previous = [
            r
            for r in rows
            if r["campaign_id"] == campaign
            and str(previous_start) <= r["date"] < str(current_start)
        ]
        totals = {k: math.fsum(r[k] for r in current) for k in ("impressions", "clicks", "spend")}
        ctr = ratio(totals["clicks"], totals["impressions"])
        scope = f"campaign_id={campaign} AND date BETWEEN DATE '{current_start}' AND DATE '{end}'"
        prefix = f"人工日期 {current_start} 至 {end}（含首尾日），Campaign {campaign}，"
        families: list[tuple[str, str, str, list[dict[str, Any]], bool]]
        if split == "dev":
            families = [
                (
                    "totals",
                    "查询曝光数 impressions、点击数 clicks、变换费用 spend 的总量。",
                    f"SELECT SUM(impressions) AS impressions,SUM(clicks) AS clicks,SUM(spend) AS spend FROM daily_metrics WHERE {scope}",
                    [totals],
                    False,
                ),
                (
                    "ctr",
                    "查询整段期间的点击率 ctr，输出小数比率。",
                    f"SELECT SUM(clicks)*1.0/NULLIF(SUM(impressions),0) AS ctr FROM daily_metrics WHERE {scope}",
                    [{"ctr": ctr}],
                    False,
                ),
                (
                    "daily",
                    "按日期升序列出每日 date、impressions、clicks、spend。",
                    f"SELECT date,impressions,clicks,spend FROM daily_metrics WHERE {scope} ORDER BY date",
                    [
                        {k: r[k] for k in ("date", "impressions", "clicks", "spend")}
                        for r in current
                    ],
                    True,
                ),
                (
                    "top_click_days",
                    "找点击数最多的3天，输出 date、clicks；按点击降序，同点击按日期升序。",
                    f"SELECT date,clicks FROM daily_metrics WHERE {scope} ORDER BY clicks DESC,date ASC LIMIT 3",
                    [
                        {"date": r["date"], "clicks": r["clicks"]}
                        for r in sorted(current, key=lambda r: (-r["clicks"], r["date"]))[:3]
                    ],
                    True,
                ),
            ]
        else:
            before = ratio(
                math.fsum(r["clicks"] for r in previous),
                math.fsum(r["impressions"] for r in previous),
            )
            delta = (ctr - before) * 100 if ctr is not None and before is not None else None

            def expression(field: str, condition: str) -> str:
                return f"SUM(CASE WHEN {condition} THEN {field} ELSE 0 END)"

            cur_condition = f"date >= DATE '{current_start}'"
            prev_condition = f"date < DATE '{current_start}'"
            delta_sql = f"SELECT 100.0*({expression('clicks', cur_condition)}*1.0/NULLIF({expression('impressions', cur_condition)},0)-{expression('clicks', prev_condition)}*1.0/NULLIF({expression('impressions', prev_condition)},0)) AS ctr_change_pp FROM daily_metrics WHERE campaign_id={campaign} AND date BETWEEN DATE '{previous_start}' AND DATE '{end}'"
            families = [
                (
                    "cpc",
                    "查询整段期间每次点击的平均变换费用 cpc，零点击时返回 NULL。",
                    f"SELECT SUM(spend)/NULLIF(SUM(clicks),0) AS cpc FROM daily_metrics WHERE {scope}",
                    [{"cpc": ratio(totals["spend"], totals["clicks"])}],
                    False,
                ),
                (
                    "daily_cpc_filter",
                    "列出点击数至少10的日期及当日 cpc，输出 date、cpc，按日期升序。",
                    f"SELECT date,SUM(spend)/NULLIF(SUM(clicks),0) AS cpc FROM daily_metrics WHERE {scope} GROUP BY date HAVING SUM(clicks)>=10 ORDER BY date",
                    [
                        {"date": r["date"], "cpc": ratio(r["spend"], r["clicks"])}
                        for r in current
                        if r["clicks"] >= 10
                    ],
                    True,
                ),
                (
                    "period_ctr_delta",
                    f"与紧邻上期 {previous_start} 至 {current_start - timedelta(days=1)} 比较，本期 CTR 减去上期 CTR 是多少个百分点？仅输出 ctr_change_pp。",
                    delta_sql,
                    [{"ctr_change_pp": delta}],
                    False,
                ),
                (
                    "empty_filter",
                    "列出曝光数小于0的记录，输出 date、impressions，按日期升序。",
                    f"SELECT date,impressions FROM daily_metrics WHERE {scope} AND impressions<0 ORDER BY date",
                    [],
                    True,
                ),
            ]
        for family, question, oracle, expected, ordered in families:
            tasks.append(
                {
                    "id": f"{split}-{campaign}-{family}",
                    "split": split,
                    "family": family,
                    "campaign_id": campaign,
                    "start_date": str(current_start),
                    "end_date": str(end),
                    "question": prefix + question,
                    "oracle_sql": oracle,
                    "expected_rows": expected,
                    "ordered": ordered,
                    "label_origin": "developer_template_independent_python_reference",
                    "version": VERSION,
                }
            )
    return tasks


def validate_data(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest["schema"] != SCHEMA or not manifest["validation"]["independent_python_aggregation"]:
        raise ValueError("Dataset schema or independent validation missing")
    if manifest["data_hash"] != sha256(root / "campaignops.duckdb") or manifest[
        "reference_hash"
    ] != sha256(root / "reference.csv"):
        raise ValueError("Dataset/reference fingerprint mismatch")
    return dict(manifest)


async def evaluate(
    root: Path,
    output: Path,
    mode: str,
    split: str,
    limit: int | None,
    concurrency: int,
    variant: str,
    suite: str = "regression",
) -> dict[str, Any]:
    manifest = validate_data(root)
    all_tasks = make_tasks(root)
    if suite == "expanded":
        from scripts.criteo_tasks import make_expanded_tasks

        all_tasks = make_expanded_tasks(reference_rows(root), {t["campaign_id"] for t in all_tasks})
    elif suite != "regression":
        raise ValueError("Unknown suite")
    tasks = [t for t in all_tasks if split == "all" or t["split"] == split]
    if limit is not None:
        tasks = tasks[:limit]
    settings = Settings(
        data_dir=root, database_backend="duckdb", redis_url=SecretStr(""), max_query_rows=200
    )
    # Verify the reference SQL against independently aggregated Python answers BEFORE any model call.
    executor = SQLExecutor(settings, schema=SCHEMA)
    try:
        for task in tasks:
            result = executor.execute(task["oracle_sql"])
            if (
                result.data is None
                or result.data.row_count >= settings.max_query_rows
                or not same_results(result.data.rows, task["expected_rows"], task["ordered"])
            ):
                raise ValueError(f"Oracle verification failed: {task['id']}")
            task["expected_columns"] = result.data.columns
    finally:
        executor.cache.close()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "tasks.json", tasks)
    system = instructions(variant)
    (output / "prompt.txt").write_text(system, encoding="utf-8")
    metadata = {
        "scope": "SQL planning + guarded execution component; NOT full workflow or root-cause evaluation",
        "version": tasks[0]["version"],
        "suite": suite,
        "mode": mode,
        "split": split,
        "limit": limit,
        "model": settings.model_name if mode != "verify" else None,
        "prompt_variant": variant,
        "prompt_hash": hashlib.sha256(system.encode()).hexdigest(),
        "tasks_hash": sha256(output / "tasks.json"),
        "data_hash": manifest["data_hash"],
        "reference_hash": manifest["reference_hash"],
        "dataset_revision": manifest["revision"],
        "raw_rows": manifest["raw_rows"],
        "model_timeout_seconds": settings.model_timeout_seconds,
        "model_max_tokens": settings.model_max_tokens,
        "temperature": 0,
        "retries": 0,
        "concurrency": concurrency,
        "source_hashes": {
            name: sha256(Path(name))
            for name in (
                "scripts/evaluate_criteo.py",
                "scripts/prepare_criteo.py",
                "scripts/criteo_tasks.py",
                "scripts/evaluate.py",
                "app/agent/runners.py",
                "app/guardrails/sql.py",
                "app/tools/sql.py",
            )
        },
    }
    write_json(output / "metadata.json", metadata)
    semaphore = asyncio.Semaphore(concurrency)

    async def run(task: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            started = time.perf_counter()
            row: dict[str, Any] = {
                "id": task["id"],
                "family": task["family"],
                "split": task["split"],
                "question": task["question"],
                "expected_rows": task["expected_rows"],
                "expected_columns": task["expected_columns"],
                "oracle_sql": task["oracle_sql"],
                "passed": False,
                "oracle_verified": True,
            }
            runner = None
            sql_executor = SQLExecutor(settings, schema=SCHEMA)
            try:
                if mode == "verify":
                    row.update(passed=True, status="oracle_verified_only")
                else:
                    runner = (
                        OpenAICompatibleRunner(settings, system_instructions=system)
                        if mode == "openai"
                        else YoutuRunner(settings, system_instructions=system)
                    )
                    start, end = (
                        date.fromisoformat(task["start_date"]),
                        date.fromisoformat(task["end_date"]),
                    )
                    context = TaskContext(
                        "query",
                        start,
                        end,
                        start - timedelta(days=(end - start).days + 1),
                        task["campaign_id"],
                        [],
                    )
                    async with asyncio.timeout(settings.model_timeout_seconds + 5):
                        plan = await runner.plan(task["question"], context, [])
                    row["plan"] = plan.model_dump(mode="json")
                    if plan.start_date != start or plan.end_date != end:
                        raise ValueError("Model plan date contract mismatch")
                    result = await asyncio.to_thread(sql_executor.execute, plan.sql)
                    assert result.data is not None
                    row.update(
                        actual_rows=result.data.rows,
                        actual_columns=result.data.columns,
                        normalized_sql=result.data.normalized_sql,
                        query_hash=result.data.query_hash,
                    )
                    row["passed"] = (
                        result.data.row_count < settings.max_query_rows
                        and set(task["expected_columns"]) <= set(result.data.columns)
                        and same_results(result.data.rows, task["expected_rows"], task["ordered"])
                    )
                    row["status"] = "passed" if row["passed"] else "result_mismatch"
            except AppError as exc:
                row.update(status="error", error_code=exc.info.code, error=exc.info.message)
            except Exception as exc:
                # Never include arbitrary provider/HTTP exception text or credentials in artifacts.
                row.update(status="error", error_code=type(exc).__name__)
            finally:
                sql_executor.cache.close()
            if runner is not None:
                row["usage"] = runner.metrics.model_dump(mode="json")
            row["duration_ms"] = (time.perf_counter() - started) * 1000
            with (output / "results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            print(f"{row['id']}: {row['status']}", flush=True)
            return row

    results = await asyncio.gather(*(run(task) for task in tasks))
    passed = sum(r["passed"] for r in results)
    metrics = {
        "passed": passed,
        "total": len(results),
        "execution_accuracy": passed / len(results) if mode != "verify" else None,
        "oracle_checks_passed": len(tasks),
        "by_split": {
            part: {
                "passed": sum(r["passed"] for r in results if r["split"] == part),
                "total": sum(r["split"] == part for r in results),
            }
            for part in sorted({r["split"] for r in results})
        },
        "by_family": {
            family: {
                "passed": sum(r["passed"] for r in results if r["family"] == family),
                "total": sum(r["family"] == family for r in results),
            }
            for family in sorted({r["family"] for r in results})
        },
    }
    report = {"metadata": metadata, "metrics": metrics, "results": results}
    write_json(output / "report.json", report)
    summary = (
        f"# Criteo 查询组件评测\n\n模式：{mode}；题库：{suite}；集合：{split}；通过：{passed}/{len(results)}。\n\n"
        + (
            "仅验证数据和标准答案，未调用模型，不代表模型准确率。"
            if mode == "verify"
            else "评测 SQL 规划与安全执行，不包含 RAG、完整工作流、诊断和预算操作。"
        )
    )
    summary += (
        "\n\n题目由开发模板生成，参考值来自独立 Python 聚合，不是 Criteo 官方问答金标。\n\n| 题号 | 状态 |\n| --- | --- |\n"
        + "\n".join(f"| {r['id']} | {r['status']} |" for r in results)
        + "\n"
    )
    (output / "report.md").write_text(summary, encoding="utf-8")
    cards = "".join(
        f"<details class='task' data-status='{html.escape(r['status'], quote=True)}' data-family='{html.escape(r['family'], quote=True)}'><summary>{html.escape(r['id'])} — {html.escape(r['status'])}</summary><pre>{html.escape(json.dumps(r, ensure_ascii=False, indent=2, default=str))}</pre></details>"
        for r in results
    )
    family_options = "".join(
        f"<option>{html.escape(name)}</option>" for name in metrics["by_family"]
    )
    family_table = (
        "<table><tr><th>题型</th><th>通过/总数</th></tr>"
        + "".join(
            f"<tr><td>{html.escape(name)}</td><td>{v['passed']}/{v['total']}</td></tr>"
            for name, v in metrics["by_family"].items()
        )
        + "</table>"
    )
    controls = (
        '<p><label>筛选问题 <input id="search" placeholder="输入题号或问题关键词"></label> <label>状态 <select id="status"><option value="">全部</option><option value="failed">仅失败</option><option value="passed">仅通过</option></select></label> <label>题型 <select id="family"><option value="">全部题型</option>'
        + family_options
        + '</select></label> <span id="count"></span></p>'
    )
    filtering = """<script>
const cards=[...document.querySelectorAll('details.task')];
function filter(){const query=document.getElementById('search').value.toLowerCase(),status=document.getElementById('status').value,family=document.getElementById('family').value;let visible=0;for(const card of cards){const passed=['passed','oracle_verified_only'].includes(card.dataset.status);card.hidden=!(card.textContent.toLowerCase().includes(query)&&(!family||family===card.dataset.family)&&(!status||(status==='passed'?passed:!passed)));if(!card.hidden)visible++;}document.getElementById('count').textContent=visible+' / '+cards.length+' 题';}
for(const id of ['search','status','family'])document.getElementById(id).addEventListener('input',filter);filter();
</script>"""
    (output / "review.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Criteo 评测证据</title><style>body{max-width:1100px;margin:40px auto;font:16px system-ui;padding:0 20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:16px}details{margin:12px 0}summary{cursor:pointer}td,th{padding:4px 16px;text-align:left}input,select{padding:6px}</style><h1>Criteo 查询组件评测</h1>"
        + family_table
        + "<details><summary>完整报告与题目索引</summary><pre>"
        + html.escape(summary)
        + "</pre></details>"
        + controls
        + cards
        + filtering
        + "</html>",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/external/criteo"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["verify", "openai", "youtu"], default="verify")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="test")
    parser.add_argument("--suite", choices=["regression", "expanded"], default="regression")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--prompt-variant", choices=["semantic", "plain"], default="semantic")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    report = asyncio.run(
        evaluate(
            args.data_dir,
            args.output,
            args.mode,
            args.split,
            args.limit,
            args.concurrency,
            args.prompt_variant,
            args.suite,
        )
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    if report["metrics"]["passed"] != report["metrics"]["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
