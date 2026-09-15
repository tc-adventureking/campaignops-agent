"""Build a frozen 110k business-query dataset; raw log rows never count as QA samples."""

import argparse
import hashlib
import json
import random
import shutil
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from scripts.criteo_tasks import make_expanded_tasks, scenario_tasks
from scripts.evaluate_criteo import make_tasks, reference_rows, validate_data
from scripts.prepare_criteo import sha256, write_json

VERSION = "campaignops-large-v1"
COUNTS = {"train": 88_000, "dev": 10_000, "test": 12_000}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def partition_campaigns(rows: list[dict[str, Any]], excluded: set[int]) -> dict[str, list[int]]:
    candidates = sorted(
        {r["campaign_id"] for r in rows} - excluded, key=lambda c: digest([VERSION, c])
    )
    if len(candidates) < 240:
        raise ValueError("Need at least 240 unseen Campaigns for disjoint splits and reserve")
    return {
        "train": candidates[:-200],
        "dev": candidates[-200:-120],
        "test": candidates[-120:-40],
        "reserve": candidates[-40:],
    }


def refreshed_partitions(base: dict[str, Any]) -> tuple[dict[str, list[int]], set[int]]:
    """Retire the inspected test Campaigns; only the original reserve becomes the new test."""
    if base["version"] != VERSION or len(base["campaign_partitions"]["reserve"]) < 10:
        raise ValueError("A v1 dataset with an unused reserve is required")
    old = base["campaign_partitions"]
    excluded = set(base["excluded_previously_used_campaigns"]) | set(old["test"])
    parts = {"train": old["train"], "dev": old["dev"], "test": old["reserve"], "reserve": []}
    sets = [set(v) for v in parts.values()]
    if sum(map(len, sets)) != len(set().union(*sets)) or set().union(*sets) & excluded:
        raise ValueError("Refresh would leak Campaigns across splits")
    return parts, excluded


def build(
    data_dir: Path,
    output: Path,
    counts: dict[str, int] | None = None,
    *,
    base_dataset: Path | None = None,
) -> dict[str, Any]:
    counts = COUNTS if counts is None else counts
    if set(counts) != {"train", "dev", "test"} or any(n < 20 or n % 20 for n in counts.values()):
        raise ValueError("Each split count must be positive and divisible by 20")
    source = validate_data(data_dir)
    rows = reference_rows(data_dir)
    old = make_tasks(data_dir)
    excluded = {t["campaign_id"] for t in old}
    for task in make_expanded_tasks(rows, excluded):
        excluded.update(task["campaign_ids"])
    partitions = partition_campaigns(rows, excluded)
    base = None
    version = VERSION
    if base_dataset is not None:
        base = json.loads((base_dataset / "manifest.json").read_text())
        if (
            base["data_hash"] != source["data_hash"]
            or base["reference_hash"] != source["reference_hash"]
        ):
            raise ValueError("Refresh source differs from original dataset")
        for split, part in base["splits"].items():
            if part["count"] != counts[split]:
                raise ValueError("Refresh must preserve split sizes")
            for kind in ("questions", "labels"):
                if sha256(base_dataset / part[f"{kind}_file"]) != part[f"{kind}_sha256"]:
                    raise ValueError("Original frozen file was modified")
        partitions, excluded = refreshed_partitions(base)
        version = "campaignops-large-v2"
    earliest = date.fromisoformat(source["start_date"]) + timedelta(days=1)
    latest = date.fromisoformat(source["end_date"]) - timedelta(days=1)
    by_campaign: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_campaign[row["campaign_id"]].append(row)
    output.mkdir(parents=True, exist_ok=False)
    write_json(
        output / "BUILDING.json",
        {"version": version, "requested_counts": counts, "source_hash": source["data_hash"]},
    )
    observed_ids: set[str] = set()
    observed_questions: set[str] = set()
    observed_semantics: set[str] = set()
    records: dict[str, Any] = {}
    for split, count in counts.items():
        if base is not None and base_dataset is not None and split != "test":
            part = base["splits"][split]
            for kind in ("questions", "labels"):
                shutil.copyfile(base_dataset / part[f"{kind}_file"], output / part[f"{kind}_file"])
            with (output / part["labels_file"]).open() as handle:
                actual_count = 0
                for line in handle:
                    label = json.loads(line)
                    keys = (label["id"], digest(label["question"]), digest(label["oracle_sql"]))
                    for key, seen in zip(
                        keys, (observed_ids, observed_questions, observed_semantics), strict=True
                    ):
                        if key in seen:
                            raise ValueError("Duplicate in preserved split")
                        seen.add(key)
                    actual_count += 1
            if actual_count != count:
                raise ValueError("Preserved split count mismatch")
            records[split] = part
            print(f"{split}: preserved {count} frozen samples", flush=True)
            continue
        rng = random.Random(int(digest([version, split]), 16))
        scopes: set[str] = set()
        families: Counter[str] = Counter()
        windows: Counter[int] = Counter()
        sizes: Counter[int] = Counter()
        empty, nullable = 0, 0
        questions_path, labels_path = (
            output / f"{split}.questions.jsonl",
            output / f"{split}.labels.jsonl",
        )
        with (
            questions_path.open("w", encoding="utf-8") as questions,
            labels_path.open("w", encoding="utf-8") as labels,
        ):
            group = 0
            while group < count // 20:
                days = rng.choice([1, 2, 3, 4, 5, 7, 10, 14])
                valid_first_end = earliest + timedelta(days=2 * days - 1)
                if valid_first_end > latest:
                    continue
                end = valid_first_end + timedelta(
                    days=rng.randrange((latest - valid_first_end).days + 1)
                )
                ids = sorted(rng.sample(partitions[split], rng.choice([2, 3, 4, 6, 8, 10])))
                scope_key = digest([ids, str(end), days])
                if scope_key in scopes:
                    continue
                scopes.add(scope_key)
                selected = [r for c in ids for r in by_campaign[c]]
                tasks = scenario_tasks(
                    selected,
                    ids,
                    group,
                    end,
                    window_days=days,
                    end_override=end,
                    top_k=rng.choice([1, 2, 3, 5]),
                    min_impressions=rng.choice([0, 10, 50, 100, 500, 1000]),
                    min_active_days=rng.choice([1, 2, 3, 5, 7]),
                )
                assert len(tasks) == 20
                for task in tasks:
                    # Separate verbalization across splits, without changing the question's meaning.
                    original_prefix, body = task["question"].split("：", 1)
                    ids_text = ",".join(map(str, ids))
                    if split == "train":
                        question = original_prefix + "：" + body
                    elif split == "dev":
                        question = f"仅统计广告计划 {ids_text}。人工日期区间为{task['start_date']}到{task['end_date']}，包含两端。请完成：{body}"
                    else:
                        question = f"请针对 Campaign 集合 [{ids_text}] 回答以下查询。人工时间范围：{task['start_date']}—{task['end_date']}（首尾均计入）；以下要求均以该范围为准，明确指定的上期除外。{body}"
                    prefix = "large" if base is None else "large-v2"
                    task_id = f"{prefix}-{split}-{group:05d}-{task['family']}"
                    question_key = digest(question)
                    semantic_key = digest(task["oracle_sql"])
                    if (
                        task_id in observed_ids
                        or question_key in observed_questions
                        or semantic_key in observed_semantics
                    ):
                        raise ValueError(f"Duplicate sample: {task_id}")
                    observed_ids.add(task_id)
                    observed_questions.add(question_key)
                    observed_semantics.add(semantic_key)
                    public = {
                        "id": task_id,
                        "question": question,
                        "start_date": task["start_date"],
                        "end_date": task["end_date"],
                        "campaign_id": None,
                    }
                    label = {
                        **task,
                        "id": task_id,
                        "question": question,
                        "split": split,
                        "version": version,
                        "scenario_group": scope_key,
                        "question_sha256": question_key,
                        "semantic_sha256": semantic_key,
                    }
                    questions.write(json.dumps(public, ensure_ascii=False, allow_nan=False) + "\n")
                    labels.write(json.dumps(label, ensure_ascii=False, allow_nan=False) + "\n")
                    families[task["family"]] += 1
                    windows[days] += 1
                    sizes[len(ids)] += 1
                    empty += not task["expected_rows"]
                    nullable += any(
                        v is None for row in task["expected_rows"] for v in row.values()
                    )
                group += 1
                if group % 100 == 0:
                    print(f"{split}: {group * 20}/{count} samples", flush=True)
        records[split] = {
            "count": count,
            "scenario_groups": len(scopes),
            "questions_file": questions_path.name,
            "questions_sha256": sha256(questions_path),
            "labels_file": labels_path.name,
            "labels_sha256": sha256(labels_path),
            "families": dict(families),
            "window_days": dict(windows),
            "campaign_group_sizes": dict(sizes),
            "empty_results": empty,
            "contains_null": nullable,
        }
    manifest = {
        "version": version,
        "total_samples": sum(counts.values()),
        "splits": records,
        "data_hash": source["data_hash"],
        "reference_hash": source["reference_hash"],
        "source_dataset": source["dataset"],
        "source_revision": source["revision"],
        "source_license": source["license"],
        "campaign_partitions": partitions,
        "excluded_previously_used_campaigns": sorted(excluded),
        "unique_ids": len(observed_ids),
        "unique_questions": len(observed_questions),
        "unique_sql_queries": len(observed_semantics),
        "label_origin": "developer_defined_templates_with_independent_python_reference_values",
        "scope": "20 business SQL families; NOT 110000 independently authored scenarios or full-agent evaluation",
        "limitations": [
            "Templates are shared across splits; Campaigns and scenario groups are disjoint.",
            "Rows share source data and templates, so binomial independence is not established.",
            "No model accuracy has been measured by building this dataset.",
        ],
        "generator_hashes": {
            p: sha256(Path(p)) for p in ("scripts/build_large_eval.py", "scripts/criteo_tasks.py")
        },
    }
    if base_dataset is not None:
        manifest["refresh_provenance"] = {
            "base_manifest_sha256": sha256(base_dataset / "manifest.json"),
            "preserved_splits": ["train", "dev"],
            "new_test_origin": "previously unused v1 reserve Campaigns",
            "retired_test": "v1 test is retained separately as an inspected regression baseline",
            "remaining_unseen_reserve_campaigns": 0,
        }
    write_json(output / "manifest.json", manifest)
    (output / "BUILDING.json").unlink()
    print(f"Built {manifest['total_samples']} unique samples; model accuracy remains unmeasured.")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/external/criteo"))
    parser.add_argument("--output", type=Path, default=Path("data/external/criteo-large-v1"))
    parser.add_argument("--refresh-test-from", type=Path)
    args = parser.parse_args()
    build(args.data_dir, args.output, base_dataset=args.refresh_test_from)


if __name__ == "__main__":
    main()
