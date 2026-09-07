"""보고서 기간 해석 테스트.

2026-09-07 실측(한화생명 2024):
- 3분기보고서 thstrm_amount 596억(3개월), thstrm_add_amount 7,270억(누적 9개월)
- 사업보고서   thstrm_amount 8,660억(연간 전체), thstrm_add_amount 없음

따라서 사업보고서의 손익을 분기값으로 쓰면 연간치를 분기로 오인한다.
재무상태표는 시점 값이므로 이 문제와 무관하다.
"""

import pytest

from fingate.warehouse.periods import PeriodKind, quarter_of, resolve_amount


def test_maps_report_codes_to_quarters():
    assert quarter_of("11013") == 1
    assert quarter_of("11012") == 2
    assert quarter_of("11014") == 3
    assert quarter_of("11011") == 4


def test_rejects_unknown_report_code():
    with pytest.raises(ValueError, match="report code"):
        quarter_of("99999")


def test_balance_sheet_is_a_point_in_time():
    """재무상태표는 시점 값이다. 누적 개념이 없다."""
    amount, kind, cumulative = resolve_amount("BS", "11011", "160,153,000,000,000", "")
    assert kind is PeriodKind.POINT
    assert amount == 160_153_000_000_000
    assert cumulative is None


def test_quarterly_income_is_three_months():
    amount, kind, cumulative = resolve_amount("IS", "11014", "59,681,021,117", "727,008,758,067")
    assert kind is PeriodKind.QUARTER
    assert amount == 59_681_021_117
    assert cumulative == 727_008_758_067


def test_annual_income_is_flagged_not_treated_as_a_quarter():
    """사업보고서 손익은 연간치다. 분기로 쓰면 4배 규모로 오인된다."""
    amount, kind, _ = resolve_amount("IS", "11011", "866,000,982,115", "")
    assert kind is PeriodKind.ANNUAL
    assert amount == 866_000_982_115


def test_annual_and_quarterly_income_are_distinguishable():
    _, annual, _ = resolve_amount("IS", "11011", "866,000,982,115", "")
    _, quarterly, _ = resolve_amount("IS", "11014", "59,681,021,117", "727,008,758,067")
    assert annual is not quarterly


def test_missing_amount_returns_none():
    amount, _, _ = resolve_amount("BS", "11011", "", "")
    assert amount is None


def test_dash_amount_returns_none():
    amount, _, _ = resolve_amount("BS", "11011", "-", "")
    assert amount is None


def test_q4_income_is_derivable_from_annual_minus_q3_cumulative():
    """DART는 4분기 손익을 직접 주지 않는다. 유도해야 한다."""
    annual, _, _ = resolve_amount("IS", "11011", "866,000,982,115", "")
    _, _, q3_cumulative = resolve_amount("IS", "11014", "59,681,021,117", "727,008,758,067")
    assert annual - q3_cumulative == 138_992_224_048
