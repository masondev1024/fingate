"""DART 계정과목 → 표준 지표 매핑.

회사·연도마다 계정명이 흔들리므로 매핑을 명시적으로 선언한다.
2026-09-07 실측에서 보험 5사 전체에 공통으로 존재한 계정만 대상으로 한다.

매핑되지 않은 계정은 조용히 버리지 않고 기록한다. 조용히 버리면 나중에
그 회사 지표가 왜 비었는지 추적할 수 없다.

IFRS17 시행(2023) 이후 계정 체계이므로 그 이전 데이터에는 적용되지 않는다.
"""

from dataclasses import dataclass
from enum import StrEnum


class Statement(StrEnum):
    BALANCE_SHEET = "BS"
    INCOME_STATEMENT = "IS"


@dataclass(frozen=True)
class Indicator:
    indicator_id: str
    name: str
    statement: Statement
    account_names: tuple[str, ...]


INDICATORS: tuple[Indicator, ...] = (
    Indicator("total_assets", "자산총계", Statement.BALANCE_SHEET, ("자산총계",)),
    Indicator("total_liabilities", "부채총계", Statement.BALANCE_SHEET, ("부채총계",)),
    Indicator("total_equity", "자본총계", Statement.BALANCE_SHEET, ("자본총계",)),
    Indicator(
        "insurance_contract_liabilities",
        "보험계약부채",
        Statement.BALANCE_SHEET,
        ("보험계약부채",),
    ),
    Indicator("retained_earnings", "이익잉여금", Statement.BALANCE_SHEET, ("이익잉여금",)),
    Indicator(
        "net_income",
        "당기순이익",
        Statement.INCOME_STATEMENT,
        ("당기순이익(손실)", "당기순이익"),
    ),
    Indicator(
        "operating_income",
        "영업이익",
        Statement.INCOME_STATEMENT,
        ("영업이익(손실)", "영업이익"),
    ),
    Indicator("interest_expense", "이자비용", Statement.INCOME_STATEMENT, ("이자비용",)),
)

_LOOKUP = {
    (indicator.statement, name): indicator
    for indicator in INDICATORS
    for name in indicator.account_names
}


def map_account(statement: str, account_name: str) -> Indicator | None:
    """계정과목을 표준 지표로 매핑한다. 매핑 대상이 아니면 None."""
    try:
        key = (Statement(statement), account_name.strip())
    except ValueError:
        return None
    return _LOOKUP.get(key)
