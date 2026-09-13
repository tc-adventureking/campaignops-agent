import json
from pathlib import Path

import duckdb
import pytest

from scripts.seed_data import seed_data

CHECKS = [
    "SELECT COUNT(*) = 4320 FROM daily_metrics",
    "SELECT COUNT(DISTINCT date) = 90 FROM daily_metrics",
    "SELECT COUNT(*) = 1 FROM accounts",
    "SELECT COUNT(*) = 6 FROM campaigns",
    "SELECT COUNT(*) = 6 FROM ad_groups",
    "SELECT COUNT(*) = 6 FROM creatives",
    "SELECT COUNT(*) = 2 FROM channels",
    "SELECT COUNT(*) = 2 FROM regions",
    "SELECT COUNT(*) = 2 FROM devices",
    "SELECT MIN(impressions) >= 0 AND MIN(clicks) >= 0 AND MIN(conversions) >= 0 FROM daily_metrics",
    "SELECT MIN(spend) >= 0 AND MIN(conversion_value) >= 0 FROM daily_metrics",
    "SELECT BOOL_AND(clicks <= impressions AND conversions <= clicks) FROM daily_metrics",
    "SELECT BOOL_AND(conversion_value = conversions * 50) FROM daily_metrics",
    "SELECT BOOL_AND(spend <= daily_budget) FROM daily_metrics",
    "SELECT COUNT(*) = 4320 FROM daily_metrics m JOIN campaigns c ON m.campaign_id=c.campaign_id JOIN creatives r ON m.creative_id=r.creative_id JOIN ad_groups a ON m.ad_group_id=a.ad_group_id WHERE c.account_id=m.account_id AND a.campaign_id=m.campaign_id AND r.ad_group_id=m.ad_group_id",
    "SELECT BOOL_AND(currency='CNY' AND timezone='Asia/Shanghai') FROM accounts",
    "SELECT COUNT(*) = 0 FROM daily_metrics WHERE date < DATE '2026-08-23' AND budget_limited=1",
]


@pytest.mark.parametrize("sql", CHECKS)
def test_data_quality(seeded_dir: Path, sql: str) -> None:
    with duckdb.connect(str(seeded_dir / "campaignops.duckdb"), read_only=True) as db:
        assert db.execute(sql).fetchone()[0]


def test_seed_reproducible(seeded_dir: Path, tmp_path: Path) -> None:
    first = json.loads((seeded_dir / "manifest.json").read_text())
    assert seed_data(tmp_path)["data_hash"] == first["data_hash"]
    assert seed_data(tmp_path, seed=123)["data_hash"] != first["data_hash"]


def test_readonly_connection_cannot_write(seeded_dir: Path) -> None:
    with duckdb.connect(str(seeded_dir / "campaignops.duckdb"), read_only=True) as db:
        with pytest.raises(duckdb.InvalidInputException):
            db.execute("DELETE FROM daily_metrics")
