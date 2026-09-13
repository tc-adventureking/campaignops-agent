"""Rebuild synthetic data atomically. Stop the API before rebuilding its database."""

import argparse
import hashlib
import json
import os
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from app.settings import Settings


def seed_data(directory: Path, seed: int | None = None) -> dict[str, Any]:
    config = json.loads(Path("data/seeds/config.json").read_text(encoding="utf-8"))
    rng = random.Random(config["seed"] if seed is None else seed)
    start = date.fromisoformat(config["start_date"])
    anomaly_start = date.fromisoformat(config["anomaly_start"])
    rows: list[tuple[Any, ...]] = []
    truth = []
    for scenario in config["scenarios"]:
        cid = scenario["campaign_id"]
        truth.append(
            {
                **scenario,
                "start_date": str(anomaly_start),
                "end_date": str(start + timedelta(days=config["days"] - 1)),
                "secondary_causes": ["cpc_rise", "cvr_drop"] if cid == 6 else [],
                "expected_evidence": [
                    scenario["metric"],
                    "channel_id" if cid == 6 else "campaign_id",
                ],
                "control_start": "2026-08-09",
                "control_end": "2026-08-15",
            }
        )
        for offset in range(config["days"]):
            day = start + timedelta(days=offset)
            active = day >= anomaly_start
            for channel in (1, 2):
                for region in (1, 2):
                    for device in (1, 2):
                        impressions = 10000 + rng.randint(-100, 100)
                        ctr, cvr, cpc = 0.04, 0.08, 1.2
                        limited = int(active and cid == 5)
                        if active:
                            if cid in (1, 5):
                                impressions = round(impressions * scenario["factor"])
                            elif cid == 2:
                                ctr *= scenario["factor"]
                            elif cid == 3:
                                cvr *= scenario["factor"]
                            elif cid == 4:
                                cpc *= scenario["factor"]
                            elif cid == 6 and channel == 2:
                                cpc *= scenario["factor"]
                                cvr *= 0.6
                        clicks = round(impressions * ctr)
                        conversions = round(clicks * cvr)
                        spend = round(clicks * cpc, 2)
                        budget = round(spend / (0.99 if limited else 0.6), 2)
                        rows.append(
                            (
                                str(day),
                                1,
                                cid,
                                cid,
                                cid,
                                channel,
                                region,
                                device,
                                impressions,
                                clicks,
                                spend,
                                conversions,
                                conversions * 50.0,
                                budget,
                                limited,
                            )
                        )
    rows.sort()
    content_hash = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "campaignops.duckdb"
    temporary = directory / "seed.tmp.duckdb"
    temporary.unlink(missing_ok=True)
    with duckdb.connect(str(temporary)) as db:
        db.execute(Path("data/schema.sql").read_text(encoding="utf-8"))
        db.execute("BEGIN TRANSACTION")
        db.execute("INSERT INTO accounts VALUES (1, '合成广告账户', 'CNY', 'Asia/Shanghai')")
        for scenario in config["scenarios"]:
            cid = scenario["campaign_id"]
            db.execute("INSERT INTO campaigns VALUES (?, 1, ?)", [cid, scenario["name"]])
            db.execute("INSERT INTO ad_groups VALUES (?, ?, ?)", [cid, cid, f"广告组{cid}"])
            db.execute("INSERT INTO creatives VALUES (?, ?, ?)", [cid, cid, f"创意{cid}"])
        db.execute("INSERT INTO channels VALUES (1, '搜索'), (2, '信息流')")
        db.execute("INSERT INTO regions VALUES (1, '深圳'), (2, '上海')")
        db.execute("INSERT INTO devices VALUES (1, '移动端'), (2, '桌面端')")
        db.executemany("INSERT INTO daily_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("COMMIT")
        db.execute("CHECKPOINT")
    os.replace(temporary, target)
    manifest = {
        "version": config["version"],
        "seed": config["seed"] if seed is None else seed,
        "rows": len(rows),
        "days": config["days"],
        "data_hash": content_hash,
        "start_date": str(start),
        "end_date": str(start + timedelta(days=config["days"] - 1)),
        "currency": "CNY",
        "timezone": "Asia/Shanghai",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (directory / "anomaly_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Settings().data_dir)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    print(json.dumps(seed_data(args.data_dir, args.seed), indent=2))


if __name__ == "__main__":
    main()
