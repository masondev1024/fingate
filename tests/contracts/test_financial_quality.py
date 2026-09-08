import pytest

from fingate.contracts.financial_quality import (
    MAX_ASSET_CHANGE,
    REQUIRED_INDICATORS,
    check_financial_quality,
    financial_series_id,
)
from fingate.warehouse.indicators import ResolvedIndicator

CORP = "00113058"
YEAR = 2024
REPORT = "11014"


def _indicator(indicator_id: str, amount: int, period_kind: str = "point") -> ResolvedIndicator:
    return ResolvedIndicator(
        indicator_id=indicator_id,
        statement="BS",
        account_nm=indicator_id,
        amount=amount,
        period_kind=period_kind,
        cumulative_amount=None,
    )


def _balanced(assets: int = 1_000, liabilities: int = 800, equity: int = 200):
    return [
        _indicator("total_assets", assets),
        _indicator("total_liabilities", liabilities),
        _indicator("total_equity", equity),
        _indicator("net_income", 50, "quarter"),
    ]


def _check(indicators, previous_total_assets=None):
    return check_financial_quality(
        CORP, YEAR, REPORT, indicators, previous_total_assets=previous_total_assets
    )


def test_a_consistent_report_passes():
    assert _check(_balanced()).ok


def test_the_series_id_separates_dart_from_ecos():
    """원장·게이트·리뷰어가 같은 식별자 공간을 쓰므로 소스가 구분되어야 한다."""
    assert financial_series_id(CORP).startswith("dart:")
    assert CORP in financial_series_id(CORP)


# --- 회계 항등식: 실측 50/50에서 오차 0으로 성립 ---


def test_assets_must_equal_liabilities_plus_equity():
    report = _check(_balanced(assets=1_000, liabilities=800, equity=100))

    assert not report.ok
    assert [f.rule for f in report.findings] == ["BALANCE_SHEET_BROKEN"]


def test_the_identity_is_checked_exactly_not_approximately():
    """실측에서 상대오차가 0.000e+00이었다. 허용 오차를 두면 실제 결함을 놓친다."""
    report = _check(
        _balanced(assets=1_000_000_000_001, liabilities=800_000_000_000, equity=200_000_000_000)
    )

    assert not report.ok


def test_the_identity_is_skipped_when_a_term_is_absent():
    """세 항 중 하나가 없으면 항등식을 검사할 수 없다. 결측은 별도 규칙이 잡는다."""
    report = _check([_indicator("total_assets", 1_000), _indicator("total_equity", 200)])

    assert "BALANCE_SHEET_BROKEN" not in [f.rule for f in report.findings]


# --- 부호: 지표마다 다르다 ---


def test_a_negative_stock_indicator_is_a_violation():
    """자산·부채·자본·잉여금·보험계약부채는 실측 음수 0건이다."""
    report = _check(_balanced(assets=-1_000, liabilities=-800, equity=-200))

    assert "NEGATIVE_STOCK" in [f.rule for f in report.findings]


@pytest.mark.parametrize("indicator_id", ["net_income", "operating_income", "interest_expense"])
def test_a_negative_flow_indicator_is_normal(indicator_id):
    """적자 분기는 실제로 존재한다. 일괄 음수 금지는 정상 데이터를 막는다.

    실측: net_income 음수 1건, operating_income 2건, interest_expense 4건.
    """
    indicators = [*_balanced(), _indicator(indicator_id, -40_000, "quarter")]

    report = check_financial_quality(CORP, YEAR, REPORT, indicators, previous_total_assets=None)

    assert "NEGATIVE_STOCK" not in [f.rule for f in report.findings]


# --- 결측: 실측 5사 x 10분기 전부 충족 ---


@pytest.mark.parametrize("missing", sorted(REQUIRED_INDICATORS))
def test_a_missing_required_indicator_is_a_violation(missing):
    indicators = [i for i in _balanced() if i.indicator_id != missing]

    report = _check(indicators)

    assert "MISSING_INDICATOR" in [f.rule for f in report.findings]
    assert missing in " ".join(f.detail for f in report.findings)


def test_an_empty_report_is_not_treated_as_passing():
    """빈 결과를 통과로 넘기면 빈 서빙이 정상이 되고 아무도 알아채지 못한다."""
    report = _check([])

    assert not report.ok
    assert [f.rule for f in report.findings] == ["NO_INDICATORS"]


# --- 총자산 급변: 실측 -2.88% ~ +8.73%, p95 6.14% ---


def test_a_normal_quarterly_change_passes():
    """실측 최대 +8.73%가 정상이다. 임계가 이보다 아래면 정상 데이터를 막는다."""
    report = _check(
        _balanced(assets=1_087, liabilities=887, equity=200), previous_total_assets=1_000
    )

    assert "ASSET_JUMP_EXCEEDED" not in [f.rule for f in report.findings]


def test_an_implausible_quarterly_change_is_a_violation():
    doubled = int(1_000 * (1 + MAX_ASSET_CHANGE * 2))
    report = _check(
        _balanced(assets=doubled, liabilities=doubled - 200, equity=200),
        previous_total_assets=1_000,
    )

    assert "ASSET_JUMP_EXCEEDED" in [f.rule for f in report.findings]


def test_the_first_quarter_has_nothing_to_compare_against():
    report = _check(_balanced(), previous_total_assets=None)

    assert "ASSET_JUMP_EXCEEDED" not in [f.rule for f in report.findings]


def test_a_zero_previous_value_does_not_divide_by_zero():
    report = _check(_balanced(), previous_total_assets=0)

    assert isinstance(report.findings, list)


# --- findings 는 게이트·리뷰어가 그대로 쓴다 ---


def test_findings_carry_the_dart_series_id_and_a_period():
    report = _check(_balanced(assets=1_000, liabilities=800, equity=100))
    finding = report.findings[0]

    assert finding.series_id == financial_series_id(CORP)
    assert finding.period is not None, "리뷰어가 시점 없는 위반은 대조하지 못한다"
