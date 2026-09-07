"""수집 대상 시계열 카탈로그 테스트.

ECOS는 같은 ITEM_CODE를 주기(D/M/Q/A)마다 별도로 제공한다. 따라서
(통계표, 항목, 주기)가 함께 있어야 시계열이 유일하게 특정된다.
"""

import pytest

from fingate.collect.series import SERIES, SeriesSpec, series_by_id


def test_every_series_has_a_unique_id():
    ids = [spec.series_id for spec in SERIES]
    assert len(ids) == len(set(ids))


def test_series_are_unique_by_table_item_and_cycle():
    keys = [(s.stat_code, s.item_code, s.cycle) for s in SERIES]
    assert len(keys) == len(set(keys))


def test_catalog_covers_policy_and_market_and_lending_rates():
    kinds = {spec.kind for spec in SERIES}
    assert {"policy", "market", "lending"} <= kinds


def test_lookup_by_id():
    spec = series_by_id("base_rate_daily")
    assert spec.stat_code == "722Y001"
    assert spec.cycle == "D"


def test_lookup_raises_for_unknown_id():
    with pytest.raises(KeyError, match="unknown series"):
        series_by_id("nope")


def test_rejects_unsupported_cycle():
    with pytest.raises(ValueError, match="cycle"):
        SeriesSpec(
            series_id="x",
            name="x",
            kind="policy",
            stat_code="722Y001",
            item_code="0101000",
            cycle="H",
            unit="%",
            freshness_sla_days=5,
        )


def test_rejects_non_positive_freshness_sla():
    with pytest.raises(ValueError, match="freshness_sla_days"):
        SeriesSpec(
            series_id="x",
            name="x",
            kind="policy",
            stat_code="722Y001",
            item_code="0101000",
            cycle="D",
            unit="%",
            freshness_sla_days=0,
        )


def test_daily_series_have_tighter_sla_than_monthly():
    """일별 지표가 월별보다 느슨한 SLA를 갖는 것은 계약 오류다."""
    daily = [s.freshness_sla_days for s in SERIES if s.cycle == "D"]
    monthly = [s.freshness_sla_days for s in SERIES if s.cycle == "M"]
    assert daily and monthly
    assert max(daily) < min(monthly)


def test_all_rate_series_are_percent():
    assert all(spec.unit == "%" for spec in SERIES)
