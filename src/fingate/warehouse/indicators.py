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


# DART는 같은 계정을 연결·별도 두 벌로 보낸다. 실측 56/56 보고서가 그랬고,
# 중복을 dict 로 접으면 응답에서 마지막에 온 행이 이긴다. 실측에서 그 규칙은
# 238/238 전부 별도재무제표를 골랐다. 아무도 그렇게 정하지 않았고 어디에도
# 기록되지 않았다. 한화생명 2024 기준 연결 160.1조 vs 별도 122.1조로 38조 차이다.
#
# 보험그룹 비교의 표준은 연결이므로 연결을 계약 기준으로 선언한다. 선언한 이상
# 없을 때 조용히 다른 기준으로 대체하지 않는다.
CONSOLIDATED = "CFS"
SEPARATE = "OFS"
DEFAULT_BASIS = CONSOLIDATED


@dataclass(frozen=True)
class ResolvedIndicator:
    """계정과목 해석 결과. 적재와 무관한 순수 값이다.

    해석을 적재 함수 안에 두면 품질 계약이 적재 전에 검사할 수 없다.
    검사하려면 먼저 적재해야 하고, 그러면 fail-closed가 성립하지 않는다.
    """

    indicator_id: str
    statement: str
    account_nm: str
    amount: int
    period_kind: str
    cumulative_amount: int | None
    # 이 값이 연결에서 왔는지 별도에서 왔는지. 기록하지 않으면 알 수 없다.
    fs_div: str = ""


@dataclass(frozen=True)
class UnmappedAccount:
    statement: str
    account_nm: str
    reason: str


def resolve_indicators(
    report_code: str, rows: list[dict], *, basis: str = DEFAULT_BASIS
) -> tuple[list[ResolvedIndicator], list[UnmappedAccount]]:
    """DART 응답 행을 표준 지표로 해석한다. 매핑 실패는 버리지 않고 함께 돌려준다.

    선언한 재무제표 기준(basis)의 행만 채택한다. 다른 기준의 행은 버리지 않고
    `OTHER_BASIS` 로 기록한다. 조용히 버리면 나중에 왜 그 지표가 그 값인지
    추적할 수 없다.

    `fs_div` 가 아예 없는 응답 형태에서는 필터하지 않는다. 필드가 없는 것과
    다른 기준인 것은 다르다.
    """
    # 지연 import: periods 가 indicators 를 참조하지 않으므로 순환은 없으나,
    # 매핑 테이블만 필요한 호출자에게 불필요한 의존을 만들지 않는다.
    from .periods import resolve_amount

    resolved: list[ResolvedIndicator] = []
    unmapped: list[UnmappedAccount] = []

    for row in rows:
        statement = str(row.get("sj_div", ""))
        account_name = str(row.get("account_nm", "")).strip()
        fs_div = str(row.get("fs_div", "") or "")

        if fs_div and fs_div != basis:
            unmapped.append(
                UnmappedAccount(statement=statement, account_nm=account_name, reason="OTHER_BASIS")
            )
            continue

        indicator = map_account(statement, account_name)
        amount, period_kind, cumulative = resolve_amount(
            statement, report_code, row.get("thstrm_amount"), row.get("thstrm_add_amount")
        )

        if indicator is None or amount is None:
            unmapped.append(
                UnmappedAccount(
                    statement=statement,
                    account_nm=account_name,
                    reason="UNMAPPED_ACCOUNT" if indicator is None else "AMOUNT_NOT_NUMERIC",
                )
            )
            continue

        resolved.append(
            ResolvedIndicator(
                indicator_id=indicator.indicator_id,
                statement=str(indicator.statement),
                account_nm=account_name,
                amount=amount,
                period_kind=str(period_kind),
                cumulative_amount=cumulative,
                fs_div=fs_div,
            )
        )
    return resolved, unmapped
