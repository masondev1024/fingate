"""앵커 관계 유도 — 어떤 계열이 다른 계열의 *수준*에 붙어 있는가.

연동 계열 대조(`peers.py`)에는 구조적 사각지대가 있었다. 동행률은 **앵커가
움직인 날**을 조건으로 계산하므로, 거의 움직이지 않는 계열은 조건이 되지 못한다.
실측에서 기준금리는 899일 중 892일 평평했다. 그 결과 `call_rate_daily` 에는
자격 peer 가 하나도 없었고, 그 계열이 차단되면 리뷰어가 영원히
`INSUFFICIENT` 만 냈다.

그런데 콜금리와 기준금리의 관계는 애초에 "함께 변한다"가 아니다. 콜금리는
정책금리 **수준을 따라가는** 전달 금리다. 관계의 종류가 다르다.

    스프레드 = 대상 - 앵커

실측 2023~2026 전 쌍의 스프레드 안정성.

    콜금리 - 기준금리          표준편차 0.0593  겹침 900   <- 사각지대를 메운다
    국고채 10y - 3y            표준편차 0.1553  겹침 901
    대출평균 - 기준금리        표준편차 0.2314  겹침 43
    기준금리 - 국고채 3y       표준편차 0.5041  겹침 900   <- 너무 헐겁다
    콜금리 - 국고채 10y        표준편차 0.6052  겹침 901   <- 너무 헐겁다

**동행률과 달리 이 관계는 대칭이다.** 콜금리 -> 기준금리 동행률은 0.012 로
탈락했지만 스프레드 표준편차는 양방향 모두 0.0593 이다.

## 임계를 왜 측정해야 했나

교과서 기본값인 3σ 를 그대로 쓰면 정상 데이터를 막는다. 콜금리 x 기준금리
스프레드는 꼬리가 두꺼워 **정상 데이터에서 최대 6.51σ** 까지 벌어졌고,
3σ 임계는 900일 중 11일(1.22%)을, 5σ 도 3일(0.33%)을 오탐한다.
반면 국고채 쌍은 최대 2.24σ 로 훨씬 좁다.

그래서 임계를 전역 상수로 두지 않고 **쌍마다 관측된 꼬리에서 유도**한다.

## 기준선은 의심 관측을 포함하면 안 된다

스프레드 표준편차를 전 구간에서 계산하면 의심 관측이 **자기가 비교당할 기준선을
오염시킨다.** 큰 이탈일수록 표준편차와 관측 최대치를 함께 키워 자신의 허용치를
넓히므로, 클수록 숨기 쉬워진다. 그래서 통계는 항상 의심 시점을 제외하고 낸다.

## 검출 가능성

스프레드가 흔들리는 만큼 급변이 묻힌다. 계약 위반 크기의 변동이 임계를 넘지
못하는 앵커는 근거가 되지 못하므로 자격에서 제외한다. 기준금리 x 국고채가
그 경우다 — 급변 임계 0.75 가 표준편차 0.5041 안에 묻힌다.
"""

import datetime as dt
from dataclasses import dataclass

from ..warehouse.store import Warehouse

# 겹치는 관측이 이보다 적으면 스프레드 분포를 신뢰할 수 없다.
MIN_ANCHOR_OVERLAP = 30

# 관측된 최대 이탈에 주는 여유. 임계가 관측 최대와 같으면 그 자체로 과적합이다.
Z_MARGIN = 1.2

# 여유를 줘도 이보다 낮게는 내리지 않는다. 표본이 좁아 꼬리를 아직 못 본
# 쌍에서 임계가 지나치게 예민해지는 것을 막는다.
Z_FLOOR = 4.0


@dataclass(frozen=True)
class AnchorLink:
    """대상 계열과 그 수준을 규정하는 앵커. 판정이 아니라 측정값을 담는다."""

    series_id: str
    anchor_id: str
    overlap: int
    spread_mean: float
    spread_sd: float
    observed_max_z: float
    alert_z: float
    detectable_z: float

    @property
    def description(self) -> str:
        return (
            f"{self.anchor_id}: 스프레드 평균 {self.spread_mean:+.4f}, "
            f"표준편차 {self.spread_sd:.4f}, 관측 최대 이탈 {self.observed_max_z:.2f}σ, "
            f"경보 {self.alert_z:.2f}σ, 겹침 {self.overlap}"
        )


_ANCHOR_SQL = """
WITH paired AS (
    SELECT
        anchor.series_id AS anchor_id,
        subject.value - anchor.value AS spread
    FROM bronze_rate_observation AS subject
    JOIN bronze_rate_observation AS anchor
      ON anchor.period = subject.period
     AND anchor.series_id <> subject.series_id
    WHERE subject.series_id = ?
      AND (CAST(? AS DATE) IS NULL OR subject.period <> CAST(? AS DATE))
),
stats AS (
    SELECT anchor_id, count(*) AS overlap, avg(spread) AS mu, stddev(spread) AS sd
    FROM paired
    GROUP BY anchor_id
)
SELECT
    stats.anchor_id,
    stats.overlap,
    stats.mu,
    stats.sd,
    max(abs(paired.spread - stats.mu) / stats.sd) AS observed_max_z
FROM paired
JOIN stats ON stats.anchor_id = paired.anchor_id
WHERE stats.sd > 0
GROUP BY stats.anchor_id, stats.overlap, stats.mu, stats.sd
ORDER BY stats.anchor_id
"""


def _finite(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    return None if number != number else number


def anchors_for(
    warehouse: Warehouse,
    series_id: str,
    max_jump: float,
    *,
    exclude_period: dt.date | None = None,
) -> list[AnchorLink]:
    """이 계열의 수준을 규정하는 것으로 측정된 앵커만 돌려준다.

    `max_jump` 는 이 계열의 품질 계약상 급변 임계다. 그 크기의 변동이 스프레드
    경보를 넘기지 못하면 앵커로서 쓸모가 없으므로 제외한다.

    `exclude_period` 는 판정 대상 시점이다. 기준선 통계에서 반드시 빼야 한다.
    포함하면 이탈이 클수록 자신의 허용치를 넓혀 스스로를 숨긴다.
    """
    rows = warehouse.query(_ANCHOR_SQL, (series_id, exclude_period, exclude_period))

    links: list[AnchorLink] = []
    for row in rows:
        sd = _finite(row["sd"])
        mu = _finite(row["mu"])
        observed = _finite(row["observed_max_z"])
        if row["overlap"] < MIN_ANCHOR_OVERLAP:
            continue
        if sd is None or sd <= 0 or mu is None or observed is None:
            continue

        alert_z = max(observed * Z_MARGIN, Z_FLOOR)
        detectable_z = max_jump / sd
        if detectable_z <= alert_z:
            # 계약 위반 크기의 변동이 스프레드 잡음에 묻힌다.
            continue

        links.append(
            AnchorLink(
                series_id=series_id,
                anchor_id=row["anchor_id"],
                overlap=int(row["overlap"]),
                spread_mean=mu,
                spread_sd=sd,
                observed_max_z=observed,
                alert_z=alert_z,
                detectable_z=detectable_z,
            )
        )
    return sorted(links, key=lambda link: (-link.detectable_z, link.anchor_id))
