"""보고서 기간 해석.

DART 재무 금액은 계정 성격과 보고서 종류에 따라 의미가 다르다.
2026-09-07 한화생명 2024로 실측해 확인했다.

| 보고서 | sj_div | thstrm_amount | thstrm_add_amount |
|---|---|---|---|
| 3분기보고서 | IS | 596억 (3개월) | 7,270억 (누적 9개월) |
| 사업보고서 | IS | 8,660억 (연간 전체) | 없음 |
| 모두 | BS | 시점 잔액 | 해당 없음 |

함정이 둘이다.

- 사업보고서의 손익을 분기값으로 쓰면 연간치를 분기로 오인한다.
  실제 4분기는 연간 8,660억에서 3분기 누적 7,270억을 뺀 1,390억이다.
- thstrm_dt를 믿으면 안 된다. 3분기보고서는 기간을 2024.01.01~09.30으로
  표기하지만 thstrm_amount는 3개월치다.
"""

from enum import StrEnum

_QUARTER_BY_REPORT = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
_INTERIM_REPORTS = frozenset({"11013", "11012", "11014"})
_ANNUAL_REPORT = "11011"


class PeriodKind(StrEnum):
    POINT = "point"  # 시점 잔액 (재무상태표)
    QUARTER = "quarter"  # 해당 분기 3개월 (분기보고서 손익)
    ANNUAL = "annual"  # 연간 전체 (사업보고서 손익)


def quarter_of(report_code: str) -> int:
    if report_code not in _QUARTER_BY_REPORT:
        raise ValueError(f"unknown report code: {report_code}")
    return _QUARTER_BY_REPORT[report_code]


def _parse(raw: object) -> int | None:
    text = str(raw or "").replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def resolve_amount(
    statement: str, report_code: str, amount: object, cumulative: object
) -> tuple[int | None, PeriodKind, int | None]:
    """금액, 기간 성격, 연초 누적치를 함께 돌려준다.

    기간 성격을 함께 넘기지 않으면 하류에서 연간치와 분기치가 섞인다.
    누적치는 4분기 손익을 유도하는 데 쓴다. 사업보고서는 연간 전체만
    제공하므로 4분기 = 연간 - 3분기 누적으로 계산해야 한다.
    """
    value = _parse(amount)
    accumulated = _parse(cumulative)
    if statement == "BS":
        return value, PeriodKind.POINT, None
    if report_code == _ANNUAL_REPORT:
        return value, PeriodKind.ANNUAL, None
    if report_code in _INTERIM_REPORTS:
        return value, PeriodKind.QUARTER, accumulated
    raise ValueError(f"unknown report code: {report_code}")
