"""표준 지표 매핑 테스트."""

from fingate.warehouse.indicators import INDICATORS, Statement, map_account


def test_indicator_ids_are_unique():
    ids = [indicator.indicator_id for indicator in INDICATORS]
    assert len(ids) == len(set(ids))


def test_maps_balance_sheet_account():
    indicator = map_account("BS", "자산총계")
    assert indicator is not None
    assert indicator.indicator_id == "total_assets"


def test_maps_income_statement_variant_names():
    """계정명이 회사·연도마다 흔들리므로 변형을 함께 선언한다."""
    assert map_account("IS", "당기순이익(손실)").indicator_id == "net_income"
    assert map_account("IS", "당기순이익").indicator_id == "net_income"


def test_tolerates_surrounding_whitespace():
    assert map_account("BS", "  자산총계  ").indicator_id == "total_assets"


def test_returns_none_for_unmapped_account():
    """매핑 대상이 아님을 None으로 알린다. 호출자가 기록할 수 있어야 한다."""
    assert map_account("BS", "파생상품자산") is None


def test_returns_none_for_unknown_statement():
    assert map_account("CF", "자산총계") is None


def test_same_account_name_in_different_statements_is_distinct():
    """BS와 IS에 같은 이름이 있어도 서로 다른 지표다."""
    assert map_account("IS", "자산총계") is None


def test_insurance_specific_indicator_exists():
    """IFRS17 이후 보험계약부채는 보험사 재무의 핵심이다."""
    indicator = map_account("BS", "보험계약부채")
    assert indicator is not None
    assert indicator.statement is Statement.BALANCE_SHEET
