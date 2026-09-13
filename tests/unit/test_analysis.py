import pytest

from app.domain.models import AppError
from app.domain.semantics import METRIC_FIELDS
from app.tools.analysis import analyze


def row(period: str, clicks: int = 400, days: int = 7) -> dict:
    return dict(
        period=period,
        observed_days=days,
        campaign_id=1,
        channel_id=1,
        region_id=1,
        device_id=1,
        impressions=10000,
        clicks=clicks,
        conversions=32,
        spend=480,
        conversion_value=1600,
        daily_budget=800,
        budget_limited=0,
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [row("current")],
        [row("current", clicks=10), row("previous")],
        [row("current", days=6), row("previous")],
    ],
)
def test_insufficient_data(rows: list[dict]) -> None:
    result = analyze(rows, 7)
    assert not result.data.sufficient_data
    assert not result.data.root_causes


def test_bad_shape() -> None:
    with pytest.raises(AppError):
        analyze([{"cvr": 0.3}], 7)


def test_dimension_mismatch() -> None:
    before = row("previous")
    before["channel_id"] = 2
    assert not analyze([row("current"), before], 7).data.sufficient_data


@pytest.mark.parametrize("present_period", [None, "current", "previous"])
def test_unobserved_period_is_unknown_not_zero(present_period: str | None) -> None:
    rows = [row(present_period)] if present_period else []
    analysis = analyze(rows, 7).data
    assert analysis is not None
    for period in ("current", "previous"):
        values = getattr(analysis, period)
        if period == present_period:
            assert values["impressions"] == 10000
        else:
            assert all(value is None for value in values.values())
    assert all(
        change["absolute"] is None and change["relative"] is None
        for change in analysis.changes.values()
    )
    assert not analysis.sufficient_data
    assert not analysis.root_causes


def test_observed_zero_is_preserved_without_overriding_sample_gate() -> None:
    zero = row("current")
    zero.update(dict.fromkeys(METRIC_FIELDS, 0))
    analysis = analyze([zero, row("previous")], 7).data
    assert analysis is not None
    assert all(analysis.current[metric] == 0 for metric in METRIC_FIELDS)
    assert analysis.current["ctr"] is None
    assert analysis.changes["impressions"] == {"absolute": -10000, "relative": -1}
    assert not analysis.sufficient_data
    assert not analysis.root_causes
