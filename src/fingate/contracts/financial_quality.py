"""재무 지표 품질 계약.

금리에는 3계층 계약이 있었지만 보험사 재무는 응답 계약만 통과하고 곧바로
적재되고 있었다. 서빙 테이블 50행 전부가 이 재무 데이터를 구동 테이블로
삼으므로, 게이트가 걸린 쪽은 오히려 LEFT JOIN 되는 맥락(금리)뿐이었다.

임계는 전부 2023~2026 적재분 실측에서 유도했다. 추측으로 정하면 정상 데이터를
거부하거나 이상치를 놓친다.

## 측정이 정한 것

**회계 항등식은 정확히 성립한다.** 자산 = 부채 + 자본이 검사 가능한 50개
회사-분기 전부에서 상대오차 0.000e+00으로 맞았다. 허용 오차를 두면 실제
결함을 놓치므로 정확 비교한다.

**부호 규칙은 지표마다 다르다.** 자산·부채·자본·이익잉여금·보험계약부채는
실측 음수 0건이다. 그러나 손익과 이자비용은 음수가 실제로 존재한다
(net_income 1건, operating_income 2건, interest_expense 4건). 적자 분기이므로
정상이다. 일괄 "음수 금지"는 정상 데이터를 막는다.

**총자산 분기 변동은 -2.88% ~ +8.73%였다.** 평균 절대변동 2.61%, p95 6.14%.
임계를 관측 최대보다 아래에 두면 정상 분기가 막힌다. 관측 최대의 약 3배에 둔다.

**5사 x 10분기에서 총자산·자본·손익은 모두 10/10 존재했다.** 결측이 생기면
그것 자체가 이상 신호다.
"""

import datetime as dt

from ..warehouse.indicators import ResolvedIndicator
from .quality import QualityFinding, QualityReport

# 원장·게이트·리뷰어가 금리와 같은 식별자 공간을 쓰므로 소스를 구분한다.
DART_PREFIX = "dart"

# 저량(stock) 지표. 실측 음수 0건이므로 음수는 결함이다.
NON_NEGATIVE_INDICATORS = frozenset(
    {
        "total_assets",
        "total_liabilities",
        "total_equity",
        "retained_earnings",
        "insurance_contract_liabilities",
    }
)

# 유량(flow) 지표는 음수가 정상이다. 여기에 부호 규칙을 적용하지 않는다.
REQUIRED_INDICATORS = frozenset({"total_assets", "total_equity", "net_income"})

# 실측 총자산 분기 변동 최대 +8.73%, p95 6.14%. 그 위에 여유를 두되
# 보험사 총자산이 한 분기에 25% 움직이면 확실히 확인이 필요하다.
MAX_ASSET_CHANGE = 0.25

_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_REPORT_QUARTER = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def financial_series_id(corp_code: str) -> str:
    return f"{DART_PREFIX}:{corp_code}"


def report_code_of(period: dt.date) -> str:
    """분기말 날짜에서 보고서 코드를 되돌린다.

    원장에 남는 것은 위반의 시점뿐이므로, 사후에 원본과 대조하려면 어느
    보고서였는지를 시점에서 복원할 수 있어야 한다.
    """
    for code, quarter in _REPORT_QUARTER.items():
        if _QUARTER_END[quarter] == (period.month, period.day):
            return code
    raise ValueError(f"not a quarter end: {period}")


def _period_of(bsns_year: int, report_code: str) -> dt.date:
    """위반에 시점을 붙인다. 시점이 없으면 리뷰어가 대조할 대상을 특정하지 못한다."""
    month, day = _QUARTER_END[_REPORT_QUARTER.get(report_code, 4)]
    return dt.date(bsns_year, month, day)


def check_financial_quality(
    corp_code: str,
    bsns_year: int,
    report_code: str,
    indicators: list[ResolvedIndicator],
    *,
    previous_total_assets: int | None = None,
) -> QualityReport:
    series_id = financial_series_id(corp_code)
    period = _period_of(bsns_year, report_code)
    findings: list[QualityFinding] = []

    def add(rule: str, detail: str) -> None:
        findings.append(QualityFinding(series_id, rule, detail, period))

    if not indicators:
        add("NO_INDICATORS", f"{corp_code} {bsns_year} {report_code} produced no indicators")
        return QualityReport(series_id, findings, 0, period)

    amounts = {item.indicator_id: item.amount for item in indicators}

    for required in sorted(REQUIRED_INDICATORS):
        if required not in amounts:
            add("MISSING_INDICATOR", f"{required} is absent from the report")

    for item in indicators:
        if item.indicator_id in NON_NEGATIVE_INDICATORS and item.amount < 0:
            add(
                "NEGATIVE_STOCK",
                f"{item.indicator_id} is {item.amount}, which cannot be negative",
            )

    assets = amounts.get("total_assets")
    liabilities = amounts.get("total_liabilities")
    equity = amounts.get("total_equity")
    if assets is not None and liabilities is not None and equity is not None:
        # 실측 상대오차가 정확히 0이었으므로 허용 오차를 두지 않는다.
        if assets != liabilities + equity:
            add(
                "BALANCE_SHEET_BROKEN",
                f"assets {assets} != liabilities {liabilities} + equity {equity} "
                f"(gap {assets - (liabilities + equity)})",
            )

    if assets is not None and previous_total_assets:
        change = (assets - previous_total_assets) / previous_total_assets
        if abs(change) > MAX_ASSET_CHANGE:
            add(
                "ASSET_JUMP_EXCEEDED",
                f"total assets moved {change:+.1%} from {previous_total_assets} to {assets}, "
                f"limit is {MAX_ASSET_CHANGE:.0%}",
            )

    return QualityReport(series_id, findings, len(indicators), period)
