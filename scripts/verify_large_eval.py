"""Audit frozen QA files and independently execute every oracle, with resumable receipts."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import duckdb

from app.guardrails.sql import validate_sql
from scripts.evaluate import same_results
from scripts.evaluate_criteo import validate_data
from scripts.prepare_criteo import SCHEMA, sha256, write_json


def execute_rows(db: Any, sql: str) -> tuple[list[str], list[dict[str, Any]]]:
    checked = validate_sql(sql, schema=SCHEMA)
    cursor = db.execute(checked.normalized)
    columns = [str(c[0]) for c in cursor.description]
    values = cursor.fetchmany(201)
    if len(values) >= 200 or len(set(columns)) != len(columns):
        raise ValueError("Ambiguous or potentially truncated result")
    result = [dict(zip(columns, row, strict=True)) for row in values]
    return columns, list(json.loads(json.dumps(result, default=str, allow_nan=False)))


def verify(dataset: Path, data_dir: Path, output: Path) -> dict[str, Any]:
    data = validate_data(data_dir)
    manifest = json.loads((dataset / "manifest.json").read_text())
    if (
        data["data_hash"] != manifest["data_hash"]
        or data["reference_hash"] != manifest["reference_hash"]
    ):
        raise ValueError("Underlying data differs from frozen dataset")
    partitions = manifest["campaign_partitions"]
    all_sets = [set(v) for v in partitions.values()]
    if any(a & b for i, a in enumerate(all_sets) for b in all_sets[i + 1 :]):
        raise ValueError("Campaign split leakage")
    if set().union(*all_sets) & set(manifest["excluded_previously_used_campaigns"]):
        raise ValueError("Previously used Campaigns leaked into new dataset")
    for part in manifest["splits"].values():
        for kind in ("questions", "labels"):
            if sha256(dataset / part[f"{kind}_file"]) != part[f"{kind}_sha256"]:
                raise ValueError("Frozen file hash mismatch")
    fingerprint = {
        "dataset_manifest_sha256": sha256(dataset / "manifest.json"),
        "data_hash": data["data_hash"],
        "verifier_hash": sha256(Path(__file__)),
        "guard_hash": sha256(Path("app/guardrails/sql.py")),
        "scorer_hash": sha256(Path("scripts/evaluate.py")),
    }
    output.mkdir(parents=True, exist_ok=True)
    meta = output / "metadata.json"
    if meta.exists() and json.loads(meta.read_text()) != fingerprint:
        raise ValueError("Verification cannot resume after input/code changes; use a new directory")
    write_json(meta, fingerprint)
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    seen_sql: set[str] = set()
    counts: dict[str, int] = {}
    failures = 0
    with (
        sqlite3.connect(output / "verification.sqlite3") as receipts,
        duckdb.connect(
            str(data_dir / "campaignops.duckdb"),
            read_only=True,
            config={"enable_external_access": "false", "threads": "1", "memory_limit": "256MB"},
        ) as db,
    ):
        receipts.execute(
            "CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, split TEXT, label_hash TEXT, passed INTEGER, columns_json TEXT, error TEXT)"
        )
        prior = {
            r[0]: (r[1], r[2]) for r in receipts.execute("SELECT id,label_hash,passed FROM records")
        }
        for split, part in manifest["splits"].items():
            count = 0
            with (
                (dataset / part["labels_file"]).open() as labels,
                (dataset / part["questions_file"]).open() as questions,
            ):
                for label_line, question_line in zip(labels, questions, strict=True):
                    label, public = json.loads(label_line), json.loads(question_line)
                    task_id = label["id"]
                    if any(
                        public[k] != label[k]
                        for k in ("id", "question", "start_date", "end_date", "campaign_id")
                    ):
                        raise ValueError(f"Question/label mismatch: {task_id}")
                    if label["split"] != split or not set(label["campaign_ids"]) <= set(
                        partitions[split]
                    ):
                        raise ValueError(f"Split contamination: {task_id}")
                    qhash = hashlib.sha256(label["question"].encode()).hexdigest()
                    sqlhash = hashlib.sha256(label["oracle_sql"].encode()).hexdigest()
                    if task_id in seen_ids or qhash in seen_questions or sqlhash in seen_sql:
                        raise ValueError(f"Duplicate question or SQL: {task_id}")
                    seen_ids.add(task_id)
                    seen_questions.add(qhash)
                    seen_sql.add(sqlhash)
                    label_hash = hashlib.sha256(label_line.encode()).hexdigest()
                    if task_id in prior:
                        if prior[task_id][0] != label_hash:
                            raise ValueError("Resume label mismatch")
                        passed = bool(prior[task_id][1])
                    else:
                        columns: list[str] = []
                        error = None
                        try:
                            columns, actual = execute_rows(db, label["oracle_sql"])
                            passed = same_results(actual, label["expected_rows"], label["ordered"])
                            if not passed:
                                error = "Independent Python reference differs from SQL"
                        except Exception as exc:
                            passed = False
                            error = type(exc).__name__
                        receipts.execute(
                            "INSERT INTO records VALUES (?,?,?,?,?,?)",
                            (task_id, split, label_hash, int(passed), json.dumps(columns), error),
                        )
                    failures += not passed
                    count += 1
                    if count % 1000 == 0:
                        receipts.commit()
                        print(
                            f"{split}: verified {count}/{part['count']}; failures={failures}",
                            flush=True,
                        )
            if count != part["count"]:
                raise ValueError("Declared sample count differs from actual records")
            counts[split] = count
            receipts.commit()
    total = sum(counts.values())
    report = {
        "dataset_fingerprint": fingerprint,
        "actual_counts": counts,
        "total": total,
        "unique_ids": len(seen_ids),
        "unique_questions": len(seen_questions),
        "unique_sql": len(seen_sql),
        "oracle_failures": failures,
        "campaigns_disjoint": True,
        "size_gate_passed": total > 100000 and counts["test"] >= 10000,
        "all_oracles_passed": failures == 0,
        "model_accuracy": None,
    }
    if total != manifest["total_samples"]:
        raise ValueError("Dataset total mismatch")
    write_json(output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/external/criteo-large-v1"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/external/criteo"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/criteo-large-v1-verify"))
    args = parser.parse_args()
    report = verify(args.dataset, args.data_dir, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["all_oracles_passed"] or not report["size_gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
