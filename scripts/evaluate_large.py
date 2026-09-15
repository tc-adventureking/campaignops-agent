"""Resumable real-model evaluation on frozen large QA files; no oracle reaches the planner."""

import argparse
import asyncio
import hashlib
import html
import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.agent.planning import TaskContext
from app.agent.runners import OpenAICompatibleRunner, YoutuRunner
from app.domain.models import AppError
from app.settings import Settings
from app.tools.sql import SQLExecutor
from scripts.evaluate import same_results
from scripts.evaluate_criteo import instructions, validate_data
from scripts.prepare_criteo import SCHEMA, sha256, write_json


def summarize(db: sqlite3.Connection, metadata: dict[str, Any]) -> dict[str, Any]:
    completed, passed = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(passed),0) FROM results"
    ).fetchone()
    families = {
        r[0]: {"completed": r[1], "passed": r[2]}
        for r in db.execute(
            "SELECT family,COUNT(*),SUM(passed) FROM results GROUP BY family ORDER BY family"
        )
    }
    expected = metadata["selected_tasks"]
    # Unfinished requests do not disappear from the accuracy denominator.
    gate = (
        metadata["split"] == "test"
        and metadata["limit"] is None
        and expected == metadata["full_split_count"]
        and expected >= 10000
        and metadata["dataset_total"] > 100000
        and completed == expected
        and passed * 200 > expected * 199
    )
    return {
        "selected_tasks": expected,
        "completed": completed,
        "passed": passed,
        "failed": completed - passed,
        "unattempted": expected - completed,
        "accuracy": passed / expected if expected else None,
        "complete": completed == expected,
        "strictly_above_99_5_gate": gate,
        "by_family": families,
    }


def export_report(output: Path, db: sqlite3.Connection, metadata: dict[str, Any]) -> None:
    metrics = summarize(db, metadata)
    write_json(output / "report.json", {"metadata": metadata, "metrics": metrics})
    pages = output / "pages"
    pages.mkdir(exist_ok=True)
    style = "<style>body{max-width:1100px;margin:40px auto;padding:0 20px;font:16px system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f6f8;padding:16px}details{margin:14px 0}a{color:#1258aa}</style>"
    links = []
    for failed_only, prefix in ((False, "all"), (True, "failed")):
        where = " WHERE passed=0" if failed_only else ""
        cursor = db.execute("SELECT payload FROM results" + where + " ORDER BY id")
        page_number = 0
        while batch := cursor.fetchmany(100):
            page_number += 1
            name = f"{prefix}-{page_number:04d}.html"
            cards = "".join(
                "<details><summary>"
                + html.escape((r := json.loads(row[0]))["id"])
                + " — "
                + html.escape(r["status"])
                + "</summary><pre>"
                + html.escape(json.dumps(r, ensure_ascii=False, indent=2))
                + "</pre></details>"
                for row in batch
            )
            (pages / name).write_text(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>逐题证据</title>'
                + style
                + '<a href="../index.html">返回汇总</a>'
                + cards
                + "</html>",
                encoding="utf-8",
            )
            links.append(
                f'<li><a href="pages/{name}">{"失败题" if failed_only else "全部题"} 第 {page_number} 页</a></li>'
            )
    (output / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>大规模评测</title>'
        + style
        + "<h1>冻结题集真实模型评测</h1><p>按已选完整题数计分；未完成、拒绝和执行失败不算答对。开发试跑不满足最终测试验收。</p><pre>"
        + html.escape(json.dumps(metrics, ensure_ascii=False, indent=2))
        + "</pre><p>该题集为20类模板化业务查询，不代表全功能或开放域泛化准确率。</p><ul>"
        + "".join(links)
        + "</ul></html>",
        encoding="utf-8",
    )


async def evaluate(
    dataset: Path,
    data_dir: Path,
    verification: Path,
    output: Path,
    split: str,
    mode: str,
    limit: int | None,
    concurrency: int,
) -> dict[str, Any]:
    manifest = json.loads((dataset / "manifest.json").read_text())
    data = validate_data(data_dir)
    proof = json.loads((verification / "report.json").read_text())
    if not proof["all_oracles_passed"] or not proof["size_gate_passed"]:
        raise ValueError("Complete oracle/size verification required before real-model evaluation")
    if proof["dataset_fingerprint"]["dataset_manifest_sha256"] != sha256(dataset / "manifest.json"):
        raise ValueError("Dataset differs from verified manifest")
    if data["data_hash"] != manifest["data_hash"]:
        raise ValueError("Underlying database changed")
    part = manifest["splits"][split]
    for kind in ("questions", "labels"):
        if sha256(dataset / part[f"{kind}_file"]) != part[f"{kind}_sha256"]:
            raise ValueError("Frozen split file modified")
    public = [
        json.loads(line) for line in (dataset / part["questions_file"]).read_text().splitlines()
    ]
    if len(public) != part["count"] or len({p["id"] for p in public}) != len(public):
        raise ValueError("Invalid question count or duplicate IDs")
    if limit is not None:
        public = public[:limit]
    selected = {p["id"] for p in public}
    labels = {}
    with (dataset / part["labels_file"]).open() as handle:
        for line in handle:
            label = json.loads(line)
            if label["id"] in selected:
                labels[label["id"]] = label
    with sqlite3.connect(
        (verification / "verification.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    ) as verified:
        columns = {
            r[0]: json.loads(r[1])
            for r in verified.execute(
                "SELECT id,columns_json FROM records WHERE split=? AND passed=1", (split,)
            )
            if r[0] in selected
        }
    if set(columns) != selected or set(labels) != selected:
        raise ValueError("Missing verified reference labels")
    for question in public:
        if any(question[k] != labels[question["id"]][k] for k in question):
            raise ValueError("Question/reference pairing mismatch")
    settings = Settings(
        data_dir=data_dir, database_backend="duckdb", redis_url=SecretStr(""), max_query_rows=200
    )
    system = instructions("semantic")
    metadata = {
        "version": "large-evaluator-v1",
        "dataset_manifest_sha256": sha256(dataset / "manifest.json"),
        "dataset_total": manifest["total_samples"],
        "full_split_count": part["count"],
        "selected_tasks": len(public),
        "questions_sha256": part["questions_sha256"],
        "labels_sha256": part["labels_sha256"],
        "data_hash": data["data_hash"],
        "split": split,
        "mode": mode,
        "limit": limit,
        "concurrency": concurrency,
        "model": settings.model_name,
        "endpoint_hash": hashlib.sha256(settings.model_base_url.encode()).hexdigest(),
        "prompt_hash": hashlib.sha256(system.encode()).hexdigest(),
        "temperature": 0,
        "retries": 0,
        "consecutive_service_error_limit": 8,
        "model_timeout_seconds": settings.model_timeout_seconds,
        "model_max_tokens": settings.model_max_tokens,
        "source_hashes": {
            p: sha256(Path(p))
            for p in (
                "scripts/evaluate_large.py",
                "scripts/evaluate_criteo.py",
                "scripts/evaluate.py",
                "app/agent/runners.py",
                "app/tools/sql.py",
                "app/guardrails/sql.py",
            )
        },
        "scope": manifest["scope"],
        "test_gate": "All test samples >=10000; dataset >100000; correct/total strictly >0.995",
    }
    output.mkdir(parents=True, exist_ok=True)
    meta = output / "metadata.json"
    if meta.exists() and json.loads(meta.read_text()) != metadata:
        raise ValueError(
            "Cannot resume after changing model, prompt, code, data, or run parameters"
        )
    write_json(meta, metadata)
    (output / "prompt.txt").write_text(system, encoding="utf-8")
    db = sqlite3.connect(output / "results.sqlite3")
    db.execute(
        "CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,family TEXT,passed INTEGER,payload TEXT)"
    )
    completed = {r[0] for r in db.execute("SELECT id FROM results")}
    if not completed <= selected:
        raise ValueError("Unexpected stored result IDs")
    semaphore = asyncio.Semaphore(concurrency)
    service_error_streak = 0
    service_paused = asyncio.Event()

    async def run(question: dict[str, Any]) -> None:
        nonlocal service_error_streak
        async with semaphore:
            if service_paused.is_set():
                return
            task_id = question["id"]
            if task_id in completed:
                return
            label = labels[task_id]
            row: dict[str, Any] = {
                "id": task_id,
                "family": label["family"],
                "question": question["question"],
                "passed": False,
                "expected_columns": columns[task_id],
                "expected_rows": label["expected_rows"],
                "oracle_sql": label["oracle_sql"],
                "status": "pending",
            }
            started = time.perf_counter()
            runner = (
                OpenAICompatibleRunner(settings, system_instructions=system)
                if mode == "openai"
                else YoutuRunner(settings, system_instructions=system)
            )
            executor = SQLExecutor(settings, schema=SCHEMA)
            try:
                start, end = (
                    date.fromisoformat(question["start_date"]),
                    date.fromisoformat(question["end_date"]),
                )
                context = TaskContext(
                    "query",
                    start,
                    end,
                    start - timedelta(days=(end - start).days + 1),
                    question["campaign_id"],
                    [],
                )
                async with asyncio.timeout(settings.model_timeout_seconds + 5):
                    # Only user-visible question/context goes to the model, never label or family.
                    plan = await runner.plan(question["question"], context, [])
                row["plan"] = plan.model_dump(mode="json")
                if plan.start_date != start or plan.end_date != end:
                    raise ValueError("date_contract")
                result = await asyncio.to_thread(executor.execute, plan.sql)
                assert result.data is not None
                row.update(
                    actual_rows=result.data.rows,
                    actual_columns=result.data.columns,
                    normalized_sql=result.data.normalized_sql,
                )
                row["passed"] = (
                    result.data.row_count < settings.max_query_rows
                    and set(columns[task_id]) <= set(result.data.columns)
                    and same_results(result.data.rows, label["expected_rows"], label["ordered"])
                )
                row["status"] = "passed" if row["passed"] else "result_mismatch"
            except AppError as exc:
                row.update(status="error", error_code=exc.info.code, error=exc.info.message)
            except Exception as exc:
                row.update(status="error", error_code=type(exc).__name__)
            finally:
                executor.cache.close()
            row["usage"] = runner.metrics.model_dump(mode="json")
            row["duration_ms"] = (time.perf_counter() - started) * 1000
            db.execute(
                "INSERT INTO results VALUES (?,?,?,?)",
                (
                    task_id,
                    label["family"],
                    int(row["passed"]),
                    json.dumps(row, ensure_ascii=False, allow_nan=False),
                ),
            )
            db.commit()
            completed.add(task_id)
            if row.get("error_code") in {"model_timeout", "model_unavailable", "TimeoutError"}:
                service_error_streak += 1
            else:
                service_error_streak = 0
            if service_error_streak >= metadata["consecutive_service_error_limit"]:
                service_paused.set()
                write_json(
                    output / "service_pause.json",
                    {
                        "reason": "consecutive_model_service_errors",
                        "completed": len(completed),
                        "error_streak": service_error_streak,
                        "existing_failures_retained": True,
                        "resume": "Verify service health before resuming the same frozen run; only unfinished IDs resume.",
                    },
                )
            if len(completed) % 50 == 0 or len(completed) == len(public):
                progress = summarize(db, metadata)
                write_json(output / "progress.json", progress)
                print(
                    json.dumps(
                        {
                            k: progress[k]
                            for k in ("completed", "selected_tasks", "passed", "failed", "accuracy")
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    try:
        await asyncio.gather(*(run(question) for question in public))
    finally:
        db.commit()
        export_report(output, db, metadata)
        db.close()
    return dict(json.loads((output / "report.json").read_text()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/external/criteo-large-v1"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/external/criteo"))
    parser.add_argument(
        "--verification", type=Path, default=Path("artifacts/criteo-large-v1-verify")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--mode", choices=["openai", "youtu"], default="openai")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    report = asyncio.run(
        evaluate(
            args.dataset,
            args.data_dir,
            args.verification,
            args.output,
            args.split,
            args.mode,
            args.limit,
            args.concurrency,
        )
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    metrics = report["metrics"]
    if not metrics["complete"] or (
        args.split == "test" and not metrics["strictly_above_99_5_gate"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
