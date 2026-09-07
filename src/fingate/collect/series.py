"""수집 대상 시계열 카탈로그.

ECOS는 같은 ITEM_CODE를 주기별로 따로 제공한다. 예를 들어 기준금리
0101000은 연/일/월/분기 네 형태로 존재한다. 따라서 시계열을 유일하게
특정하려면 (통계표, 항목, 주기)가 모두 필요하다. 하나라도 빠지면
서로 다른 시계열이 같은 키로 섞인다.

freshness_sla_days는 설계 판단이지 업계 표준이 아니다. 기간 시작일을
기준으로 재므로 월별 지표는 월 길이까지 함께 누적된다.

일별은 영업일 기준이라 주말·연휴를 감안해 5일로 둔다.

월별은 처음에 60일로 잡았다가 2026-09-07 실측에서 정상 데이터가
STALE로 걸렸다. 예금은행 가중평균금리는 익월 말경 공표되므로 9월 초에
7월 데이터가 최신인 것이 정상이고, 기간 시작일(7/1) 기준 경과는 68일이었다.
월 길이 31일 + 공표 지연 약 40일 + 여유를 더해 95일로 조정했다.
측정 없이 정한 임계가 정상 데이터를 막은 사례다.

품질 임계는 2015~2026 실측 분포에서 도출했다. 값 범위 하한은 마이너스
금리 가능성을 최소한만 열어 두고, 상한은 실측 최대(5.64%)의 약 2.5배로
잡아 미래 금리 상승에 걸리지 않게 하되 자릿수 오류는 잡히게 한다.
급변 임계는 관측된 최대 변화폭에 여유를 준 값이다. 기준금리 빅스텝
0.50%p는 정상이므로 그보다 위에 둔다.

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
    min_value: float
    max_value: float
    max_jump: float

    def __post_init__(self) -> None:
        if self.cycle not in SUPPORTED_CYCLES:
            raise ValueError(f"unsupported cycle: {self.cycle}")
        if self.kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported kind: {self.kind}")
        if self.freshness_sla_days < 1:
            raise ValueError("freshness_sla_days must be at least 1")
        if not self.source_unit:
            raise ValueError("source_unit must not be empty")
        if self.min_value >= self.max_value:
            raise ValueError("min_value must be below max_value")
        if self.max_jump <= 0:
            raise ValueError("max_jump must be positive")


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
        min_value=-1.0,
        max_value=15.0,
        max_jump=0.75,
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
        min_value=-1.0,
        max_value=15.0,
        max_jump=0.75,
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
        min_value=-1.0,
        max_value=15.0,
        max_jump=0.75,
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
        min_value=-1.0,
        max_value=15.0,
        max_jump=1.0,
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
        freshness_sla_days=95,
        min_value=0.0,
        max_value=20.0,
        max_jump=1.0,
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
        freshness_sla_days=95,
        min_value=0.0,
        max_value=20.0,
        max_jump=1.0,
    ),
)

_INDEX = {spec.series_id: spec for spec in SERIES}


def series_by_id(series_id: str) -> SeriesSpec:
    if series_id not in _INDEX:
        raise KeyError(f"unknown series: {series_id}")
    return _INDEX[series_id]
