"""수집 대상 시계열 카탈로그.

ECOS는 같은 ITEM_CODE를 주기별로 따로 제공한다. 예를 들어 기준금리
0101000은 연/일/월/분기 네 형태로 존재한다. 따라서 시계열을 유일하게
특정하려면 (통계표, 항목, 주기)가 모두 필요하다. 하나라도 빠지면
서로 다른 시계열이 같은 키로 섞인다.

freshness_sla_days는 설계 판단이지 업계 표준이 아니다. 일별 지표는
영업일 기준이므로 주말·연휴를 감안해 여유를 둔다.
"""

from dataclasses import dataclass

SUPPORTED_CYCLES = ("D", "M", "Q", "A")
SUPPORTED_KINDS = ("policy", "market", "lending")


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    name: str
    kind: str
    stat_code: str
    item_code: str
    cycle: str
    unit: str
    freshness_sla_days: int

    def __post_init__(self) -> None:
        if self.cycle not in SUPPORTED_CYCLES:
            raise ValueError(f"unsupported cycle: {self.cycle}")
        if self.kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported kind: {self.kind}")
        if self.freshness_sla_days < 1:
            raise ValueError("freshness_sla_days must be at least 1")


SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        series_id="base_rate_daily",
        name="한국은행 기준금리",
        kind="policy",
        stat_code="722Y001",
        item_code="0101000",
        cycle="D",
        unit="%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="ktb_3y_daily",
        name="국고채(3년)",
        kind="market",
        stat_code="817Y002",
        item_code="010200000",
        cycle="D",
        unit="%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="ktb_10y_daily",
        name="국고채(10년)",
        kind="market",
        stat_code="817Y002",
        item_code="010210000",
        cycle="D",
        unit="%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="call_rate_daily",
        name="콜금리(1일, 전체거래)",
        kind="market",
        stat_code="817Y002",
        item_code="010101000",
        cycle="D",
        unit="%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="bank_loan_avg_monthly",
        name="예금은행 대출평균금리(신규취급액)",
        kind="lending",
        stat_code="121Y006",
        item_code="BECBLA01",
        cycle="M",
        unit="%",
        freshness_sla_days=60,
    ),
    SeriesSpec(
        series_id="bank_loan_household_monthly",
        name="예금은행 가계대출금리(신규취급액)",
        kind="lending",
        stat_code="121Y006",
        item_code="BECBLA03",
        cycle="M",
        unit="%",
        freshness_sla_days=60,
    ),
)

_INDEX = {spec.series_id: spec for spec in SERIES}


def series_by_id(series_id: str) -> SeriesSpec:
    if series_id not in _INDEX:
        raise KeyError(f"unknown series: {series_id}")
    return _INDEX[series_id]
