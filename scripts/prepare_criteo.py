"""Download pinned public Criteo shards and build an independently checked query fixture."""

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx

SCHEMA = {
    "daily_metrics": {
        "date": "DATE",
        "campaign_id": "INT",
        "impressions": "BIGINT",
        "clicks": "BIGINT",
        "spend": "DOUBLE",
    }
}
ANCHOR = date(2000, 1, 1)  # Explicit artificial calendar; never a real campaign date.


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def download(source: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    def one(item: dict[str, Any]) -> dict[str, Any]:
        target = raw / item["filename"]
        receipt = target.with_suffix(".receipt.json")
        if target.exists():
            if not receipt.exists():
                raise ValueError(f"Unverified existing file: {target}")
            saved = json.loads(receipt.read_text())
            if (
                saved["url"] != item["url"]
                or saved["sha256"] != sha256(target)
                or target.stat().st_size != item["size"]
            ):
                raise ValueError(f"Existing shard differs from its receipt: {target}")
            print(f"Verified {target.name}", flush=True)
            return dict(saved)
        temporary = target.with_suffix(".part")
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            with client.stream("GET", item["url"]) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
        if temporary.stat().st_size != item["size"]:
            raise ValueError(f"Incomplete shard: {temporary}")
        result = {**item, "sha256": sha256(temporary)}
        temporary.replace(target)
        write_json(receipt, result)
        print(f"Downloaded {target.name}: {item['size']} bytes", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(one, source["files"]))


def build(root: Path, source: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    destination = root / "campaignops.duckdb"
    if destination.exists() or (root / "manifest.json").exists():
        raise ValueError("Prepared dataset already exists; use a new --output directory")
    paths = [str(root / "raw" / item["filename"]) for item in receipts]
    temporary = root / "building.duckdb"
    if temporary.exists():
        raise ValueError("Incomplete build exists; inspect building.duckdb before retrying")
    with duckdb.connect(str(temporary)) as db:
        db.execute("SET threads=2")
        db.execute("SET memory_limit='512MB'")
        db.from_parquet(paths).create_view("raw_logs")
        invalid = db.execute("""SELECT COUNT(*) FROM raw_logs WHERE timestamp IS NULL OR timestamp < 0
            OR campaign IS NULL OR campaign < 0 OR click IS NULL OR click NOT IN (0,1)
            OR cost IS NULL OR NOT isfinite(cost) OR cost < 0""").fetchone()
        if invalid is None or invalid[0]:
            raise ValueError(f"Invalid source rows: {invalid}")
        db.execute(f"""CREATE TABLE daily_metrics AS SELECT
            DATE '{ANCHOR}' + CAST(floor(timestamp / 86400.0) AS INTEGER) AS date,
            CAST(campaign AS INTEGER) AS campaign_id, COUNT(*) AS impressions,
            CAST(SUM(click) AS BIGINT) AS clicks, SUM(cost) AS spend
            FROM raw_logs GROUP BY 1,2 ORDER BY 1,2""")
        rows = db.execute("SELECT * FROM daily_metrics ORDER BY date,campaign_id").fetchall()
        print(f"Built {len(rows)} daily rows; checking every group with Python", flush=True)
        # Independent aggregation of raw event records: no reuse of SQL aggregate expressions.
        reference: dict[tuple[date, int], list[float]] = defaultdict(lambda: [0, 0, 0.0])
        cursor = db.execute("SELECT timestamp,campaign,click,cost FROM raw_logs")
        raw_count = 0
        while batch := cursor.fetchmany(100_000):
            for timestamp, campaign, click, cost in batch:
                key = (ANCHOR + timedelta(days=int(timestamp) // 86400), int(campaign))
                acc = reference[key]
                acc[0] += 1
                acc[1] += int(click)
                acc[2] += float(cost)
                raw_count += 1
        if len(reference) != len(rows):
            raise ValueError("Independent aggregate group count differs")
        for day, campaign, impressions, clicks, spend in rows:
            expected = reference[(day, campaign)]
            if (
                impressions != expected[0]
                or clicks != expected[1]
                or not math.isclose(spend, expected[2], rel_tol=1e-9, abs_tol=1e-8)
            ):
                raise ValueError(f"Independent aggregate mismatch: {day}/{campaign}")
        with (root / "reference.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "campaign_id", "impressions", "clicks", "spend"])
            for (day, campaign), values in sorted(reference.items()):
                writer.writerow([day, campaign, int(values[0]), int(values[1]), values[2]])
        # Persistent view contains source paths and is unnecessary in the read-only evaluation DB.
        db.execute("DROP VIEW raw_logs")
    temporary.replace(destination)
    manifest = {
        "dataset": source["dataset"],
        "revision": source["revision"],
        "license": source["license"],
        "source": source["source"],
        "files": receipts,
        "schema": SCHEMA,
        "raw_rows": raw_count,
        "rows": len(rows),
        "campaigns": len({r[1] for r in rows}),
        "start_date": str(rows[0][0]),
        "end_date": str(rows[-1][0]),
        "calendar": "Artificial 2000-01-01 anchor; relative 86400-second days, no real timezone",
        "cost_unit": "transformed_cost_not_currency",
        "unsupported": [
            "conversions",
            "cvr",
            "cpa",
            "roas",
            "revenue",
            "budget",
            "channel",
            "region",
            "device",
            "root_cause",
        ],
        "validation": {
            "independent_python_aggregation": True,
            "groups_checked": len(rows),
            "invalid_rows": 0,
        },
        "data_hash": sha256(destination),
        "reference_hash": sha256(root / "reference.csv"),
    }
    write_json(root / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/external/criteo"))
    parser.add_argument("--source", type=Path, default=Path("configs/criteo_source.json"))
    args = parser.parse_args()
    source = json.loads(args.source.read_text())
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if (
            manifest["revision"] != source["revision"]
            or manifest["data_hash"] != sha256(args.output / "campaignops.duckdb")
            or manifest["reference_hash"] != sha256(args.output / "reference.csv")
        ):
            raise ValueError("Prepared dataset fingerprint mismatch")
        download(source, args.output)
        print("Existing dataset and all source shards verified; no files overwritten.")
        return
    receipts = download(source, args.output)
    manifest = build(args.output, source, receipts)
    print(
        json.dumps(
            {k: manifest[k] for k in ("raw_rows", "rows", "campaigns", "validation")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
