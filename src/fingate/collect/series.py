"""수집 대상 시계열 카탈로그.

ECOS는 같은 ITEM_CODE를 주기별로 따로 제공한다. 예를 들어 기준금리
0101000은 연/일/월/분기 네 형태로 존재한다. 따라서 시계열을 유일하게
특정하려면 (통계표, 항목, 주기)가 모두 필요하다. 하나라도 빠지면
서로 다른 시계열이 같은 키로 섞인다.

freshness_sla_days는 설계 판단이지 업계 표준이 아니다. 일별 지표는
영업일 기준이므로 주말·연휴를 감안해 여유를 둔다.

단위는 둘로 나눈다. source_unit은 API가 실제로 보내는 라벨이고 unit은
정규화된 의미다. 2026-09-07 실측 결과 같은 연이율인데도 대부분은 "연%"로,
가계대출금리만 "연리%"로 온다. 단위 표기가 일관되리라 가정할 수 없으므로
시계열마다 관측된 라벨을 선언하고, 라벨이 바뀌면 계약 위반으로 잡는다.
"""

from dataclasses import dataclass

SUPPORTED_CYCLES = ("D", "M", "Q", "A")
SUPPORTED_KINDS = ("policy", "market", "lending")

# 정규화된 의미 단위. 원본 라벨(연%, 연리%)이 무엇이든 이것으로 통일한다.
PERCENT_PER_ANNUM = "percent_per_annum"


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    name: str
    kind: str
    stat_code: str
    item_code: str
    cycle: str
    unit: str
    source_unit: str
    freshness_sla_days: int

    def __post_init__(self) -> None:
        if self.cycle not in SUPPORTED_CYCLES:
            raise ValueError(f"unsupported cycle: {self.cycle}")
        if self.kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported kind: {self.kind}")
        if self.freshness_sla_days < 1:
            raise ValueError("freshness_sla_days must be at least 1")
        if not self.source_unit:
            raise ValueError("source_unit must not be empty")


SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        series_id="base_rate_daily",
        name="한국은행 기준금리",
        kind="policy",
        stat_code="722Y001",
        item_code="0101000",
        cycle="D",
        unit=PERCENT_PER_ANNUM,
        source_unit="연%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="ktb_3y_daily",
        name="국고채(3년)",
        kind="market",
        stat_code="817Y002",
        item_code="010200000",
        cycle="D",
        unit=PERCENT_PER_ANNUM,
        source_unit="연%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="ktb_10y_daily",
        name="국고채(10년)",
        kind="market",
        stat_code="817Y002",
        item_code="010210000",
        cycle="D",
        unit=PERCENT_PER_ANNUM,
        source_unit="연%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="call_rate_daily",
        name="콜금리(1일, 전체거래)",
        kind="market",
        stat_code="817Y002",
        item_code="010101000",
        cycle="D",
        unit=PERCENT_PER_ANNUM,
        source_unit="연%",
        freshness_sla_days=5,
    ),
    SeriesSpec(
        series_id="bank_loan_avg_monthly",
        name="예금은행 대출평균금리(신규취급액)",
        kind="lending",
        stat_code="121Y006",
        item_code="BECBLA01",
        cycle="M",
        unit=PERCENT_PER_ANNUM,
        source_unit="연%",
        freshness_sla_days=60,
    ),
    SeriesSpec(
        series_id="bank_loan_household_monthly",
        name="예금은행 가계대출금리(신규취급액)",
        kind="lending",
        stat_code="121Y006",
        item_code="BECBLA03",
        cycle="M",
        unit=PERCENT_PER_ANNUM,
        source_unit="연리%",
        freshness_sla_days=60,
    ),
)

_INDEX = {spec.series_id: spec for spec in SERIES}


def series_by_id(series_id: str) -> SeriesSpec:
    if series_id not in _INDEX:
        raise KeyError(f"unknown series: {series_id}")
    return _INDEX[series_id]
