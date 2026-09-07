"""수집 대상 시계열 카탈로그 테스트.

ECOS는 같은 ITEM_CODE를 주기(D/M/Q/A)마다 별도로 제공한다. 따라서
(통계표, 항목, 주기)가 함께 있어야 시계열이 유일하게 특정된다.
"""

import pytest

from fingate.collect.series import (
    PERCENT_PER_ANNUM,
    SERIES,
    SeriesSpec,
    series_by_id,
)


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
            unit=PERCENT_PER_ANNUM,
            source_unit="연%",
            freshness_sla_days=5,
            min_value=-1.0,
            max_value=15.0,
            max_jump=0.75,
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
            unit=PERCENT_PER_ANNUM,
            source_unit="연%",
            freshness_sla_days=0,
            min_value=-1.0,
            max_value=15.0,
            max_jump=0.75,
        )


def test_daily_series_have_tighter_sla_than_monthly():
    """일별 지표가 월별보다 느슨한 SLA를 갖는 것은 계약 오류다."""
    daily = [s.freshness_sla_days for s in SERIES if s.cycle == "D"]
    monthly = [s.freshness_sla_days for s in SERIES if s.cycle == "M"]
    assert daily and monthly
    assert max(daily) < min(monthly)


def test_all_rate_series_normalize_to_percent_per_annum():
    assert all(spec.unit == PERCENT_PER_ANNUM for spec in SERIES)


def test_source_units_are_not_assumed_consistent():
    """같은 연이율인데도 ECOS는 "연%"와 "연리%"를 섞어 쓴다 (실측)."""
    labels = {spec.source_unit for spec in SERIES}
    assert labels == {"연%", "연리%"}


def test_rejects_inverted_value_range():
    with pytest.raises(ValueError, match="min_value"):
        SeriesSpec(
            series_id="x",
            name="x",
            kind="policy",
            stat_code="722Y001",
            item_code="0101000",
            cycle="D",
            unit=PERCENT_PER_ANNUM,
            source_unit="연%",
            freshness_sla_days=5,
            min_value=10.0,
            max_value=1.0,
            max_jump=0.75,
        )


def test_rejects_non_positive_max_jump():
    with pytest.raises(ValueError, match="max_jump"):
        SeriesSpec(
            series_id="x",
            name="x",
            kind="policy",
            stat_code="722Y001",
            item_code="0101000",
            cycle="D",
            unit=PERCENT_PER_ANNUM,
            source_unit="연%",
            freshness_sla_days=5,
            min_value=-1.0,
            max_value=15.0,
            max_jump=0.0,
        )


def test_jump_thresholds_exceed_observed_policy_big_step():
    """기준금리 빅스텝 0.50%p는 정상이므로 임계는 그보다 위여야 한다."""
    assert series_by_id("base_rate_daily").max_jump > 0.50
