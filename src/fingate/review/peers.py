"""연동 계열 유도 — 어떤 계열이 서로의 근거가 될 수 있는지 데이터에서 정한다.

승인자가 답해야 하는 질문은 "이 급변이 진짜인가"다. 가장 판별력 있는 근거는
연동된 다른 계열이 같이 움직였는지다. 진짜 금리 변동이면 연결된 계열도 반응하고,
한 계열만 튀면 그 계열의 결함일 가능성이 높다.

문제는 "어느 계열이 연동되는가"를 믿음으로 정하면 안 된다는 것이다.
2023–2026 적재분 전체 쌍을 측정한 결과 자격을 갖춘 쌍은 셋뿐이었다.

    국고채 10y ↔ 3y            Δ상관 0.911  동행률 0.922  겹침 900
    대출평균 ↔ 가계대출        Δ상관 0.744  동행률 0.763  겹침 42
    기준금리 → 콜금리          Δ상관 0.341  동행률 1.000  변동 7회

나머지는 근거가 되지 못한다. 기준금리와 국고채의 동행률은 0.429로 동전 던지기보다
나빴다. 국고채는 정책 결정을 선반영하므로 결정 당일에는 오히려 반대로 움직인다.

두 가지를 특히 조심한다.

**방향성.** 자격은 대칭이 아니다. 기준금리가 움직인 7일에 콜금리는 7/7 동행했지만,
콜금리가 움직인 수백 일 대부분 기준금리는 그대로였다. 따라서 동행률은 항상
"의심 대상 계열이 움직인 날"을 조건으로 계산한다.

**분산 0.** 기준금리 x 월별 대출금리의 상관은 nan이었다. 월 시작일에 기준금리
변동이 거의 없어 분산이 0이기 때문이다. 이때 동행률 0.000은 "완벽히 반대로
움직인다"가 아니라 "판단할 근거가 없다"이다. 변동 표본 수 하한으로 걸러낸다.

**추세 착시.** 동행률만으로는 부족하다. 둘 다 대부분의 날 오르는 계열이라면
연동되지 않아도 동행률이 높게 나온다. 그래서 peer가 자기 방향 편향만으로 우연히
맞출 확률을 기대 동행률로 계산하고, 실제 동행률이 그보다 얼마나 높은지(lift)를
함께 본다. 실측에서 자격 쌍의 lift는 +0.186 이상이었고 탈락 쌍의 최고는
+0.106이었다. 그 사이는 비어 있다.
"""

import datetime as dt
from dataclasses import dataclass

from ..warehouse.store import Warehouse

# 변화로 인정하는 최소 폭. 부동소수 잡음과 실제 변동을 가른다.
MOVE_EPSILON = 0.01

# 겹치는 관측이 이보다 적으면 어떤 동행률이 나와도 우연을 배제할 수 없다.
MIN_OVERLAP = 30

# 의심 대상 계열이 움직인 날이 이보다 적으면 동행률에 의미가 없다.
# 실측 기준금리 변동은 899일 중 7일뿐이었다. 이 하한은 그보다 낮아야 한다.
MIN_MOVE_SAMPLES = 5

# 동전 던지기(0.5)보다 확실히 높아야 근거가 된다. 실측 탈락 쌍은 0.41~0.52였고
# 자격 쌍은 0.76~1.00이었다. 그 사이에 둔다.
MIN_AGREEMENT = 0.70

# peer의 방향 편향만으로 우연히 맞출 확률을 넘어서는 몫. 실측 자격 쌍은 최소
# +0.186, 탈락 쌍은 최대 +0.106이었다. 그 사이에 둔다.
MIN_LIFT = 0.15


@dataclass(frozen=True)
class PeerLink:
    """의심 대상 계열과 연동 계열의 관계. 판정이 아니라 측정값을 담는다.

    승인자가 스스로 가중치를 정할 수 있도록 판정 근거가 된 숫자를 모두 노출한다.
    """

    series_id: str
    peer_id: str
    overlap: int
    move_samples: int
    agreement: float
    expected_agreement: float
    correlation: float | None
    noise_p90: float

    @property
    def lift(self) -> float:
        """peer의 방향 편향으로 설명되지 않는 동행. 이것이 실제 근거의 크기다."""
        return self.agreement - self.expected_agreement

    @property
    def description(self) -> str:
        correlation = "nan" if self.correlation is None else f"{self.correlation:+.3f}"
        return (
            f"{self.peer_id}: 동행률 {self.agreement:.3f} (기대 {self.expected_agreement:.3f}, "
            f"초과 {self.lift:+.3f}), 변동 {self.move_samples}회, Δ상관 {correlation}, "
            f"겹침 {self.overlap}"
        )


_PEER_SQL = """
WITH changed AS (
    SELECT
        series_id,
        period,
        value - lag(value) OVER (PARTITION BY series_id ORDER BY period) AS change
    FROM bronze_rate_observation
),
paired AS (
    SELECT
        peer.series_id AS peer_id,
        subject.change  AS subject_change,
        peer.change     AS peer_change
    FROM changed AS subject
    JOIN changed AS peer
      ON peer.period = subject.period
     AND peer.series_id <> subject.series_id
    WHERE subject.series_id = ?
      AND subject.change IS NOT NULL
      AND peer.change IS NOT NULL
      -- 변화량은 전 구간에서 계산하고(lag 이 어긋나면 안 된다) 집계에서만 제외한다.
      AND (CAST(? AS DATE) IS NULL OR subject.period <> CAST(? AS DATE))
)
SELECT
    peer_id,
    count(*) AS overlap,
    count(*) FILTER (WHERE abs(subject_change) > ?) AS move_samples,
    avg(CASE WHEN subject_change * peer_change > 0 THEN 1.0 ELSE 0.0 END)
        FILTER (WHERE abs(subject_change) > ?) AS agreement,
    corr(subject_change, peer_change) AS correlation,
    avg(CASE WHEN peer_change > 0 THEN 1.0 ELSE 0.0 END) AS peer_up_rate,
    avg(CASE WHEN subject_change > 0 THEN 1.0 ELSE 0.0 END)
        FILTER (WHERE abs(subject_change) > ?) AS subject_up_rate,
    quantile_cont(abs(peer_change), 0.90)
        FILTER (WHERE abs(subject_change) <= ?) AS noise_p90
FROM paired
GROUP BY peer_id
ORDER BY peer_id
"""


def _finite(value: object) -> float | None:
    """nan과 None을 하나로 접는다. 둘 다 '측정되지 않음'이지 0이 아니다."""
    if value is None:
        return None
    number = float(value)
    return None if number != number else number


def peers_for(
    warehouse: Warehouse, series_id: str, *, exclude_period: dt.date | None = None
) -> list[PeerLink]:
    """이 계열이 움직였을 때 함께 움직이는 것으로 측정된 계열만 돌려준다.

    자격 미달인 쌍은 아예 제외한다. 약한 근거를 약하다고 표시해 함께 보여주면
    승인자가 그것을 근거로 쓰게 된다. 근거가 없는 편이 나쁜 근거보다 낫다.

    `exclude_period` 는 판정 대상 시점이다. 기준선 통계에 의심 관측이 들어가면
    자기가 비교당할 기준을 자신이 넓히게 된다. 실측 기준금리 변동은 7회뿐이라
    한 건이 동행률에 미치는 영향이 작지 않다.
    """
    rows = warehouse.query(
        _PEER_SQL,
        (
            series_id,
            exclude_period,
            exclude_period,
            MOVE_EPSILON,
            MOVE_EPSILON,
            MOVE_EPSILON,
            MOVE_EPSILON,
        ),
    )

    links: list[PeerLink] = []
    for row in rows:
        agreement = _finite(row["agreement"])
        peer_up = _finite(row["peer_up_rate"])
        subject_up = _finite(row["subject_up_rate"])
        if row["overlap"] < MIN_OVERLAP:
            continue
        if row["move_samples"] < MIN_MOVE_SAMPLES:
            continue
        if agreement is None or agreement < MIN_AGREEMENT:
            continue
        if peer_up is None or subject_up is None:
            continue

        expected = peer_up * subject_up + (1.0 - peer_up) * (1.0 - subject_up)
        if agreement - expected < MIN_LIFT:
            continue

        links.append(
            PeerLink(
                series_id=series_id,
                peer_id=row["peer_id"],
                overlap=int(row["overlap"]),
                move_samples=int(row["move_samples"]),
                agreement=agreement,
                expected_agreement=expected,
                correlation=_finite(row["correlation"]),
                noise_p90=_finite(row["noise_p90"]) or 0.0,
            )
        )
    return sorted(links, key=lambda link: (-link.lift, link.peer_id))
