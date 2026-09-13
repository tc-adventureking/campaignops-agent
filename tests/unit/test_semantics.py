import pytest

from app.domain.semantics import aggregate, safe_ratio


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("ctr", 0.1),
        ("cvr", 0.2),
        ("cpc", 2),
        ("cpa", 10),
        ("roas", 5),
        ("impression_cvr", 0.02),
        ("budget_utilization", 0.5),
    ],
)
def test_ratios(metric: str, expected: float) -> None:
    result = aggregate(
        [
            dict(
                impressions=1000,
                clicks=100,
                conversions=20,
                spend=200,
                conversion_value=1000,
                daily_budget=400,
            )
        ]
    )
    assert result[metric] == expected


def test_weighted_aggregation() -> None:
    rows = [{"impressions": 10, "clicks": 5}, {"impressions": 1000, "clicks": 10}]
    assert aggregate(rows)["ctr"] == pytest.approx(15 / 1010)
    assert aggregate(rows)["ctr"] != (0.5 + 0.01) / 2


def test_zero_denominator() -> None:
    assert safe_ratio(0, 0) is None
    assert aggregate([])["cvr"] is None


def test_aggregation_is_partition_invariant() -> None:
    assert (
        aggregate([{"clicks": 5, "spend": 10}, {"clicks": 15, "spend": 60}])["cpc"]
        == aggregate([{"clicks": 20, "spend": 70}])["cpc"]
    )
