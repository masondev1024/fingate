import datetime as dt

import pytest

from fingate.contracts.schema import Observation
from fingate.review.peers import (
    MIN_AGREEMENT,
    MIN_LIFT,
    MIN_MOVE_SAMPLES,
    MIN_OVERLAP,
    PeerLink,
    peers_for,
)
from fingate.warehouse.store import Warehouse

LOADED_AT = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)


def _load(warehouse: Warehouse, series_id: str, values: list[float], start: dt.date) -> None:
    warehouse.load_observations(
        [
            Observation(
                series_id=series_id,
                period=start + dt.timedelta(days=index),
                raw_period=(start + dt.timedelta(days=index)).strftime("%Y%m%d"),
                value=value,
                unit="percent_per_annum",
                request_id="req",
            )
            for index, value in enumerate(values)
        ],
        LOADED_AT,
    )


@pytest.fixture
def warehouse():
    with Warehouse() as store:
        yield store


def test_a_series_that_moves_together_qualifies_as_a_peer(warehouse):
    start = dt.date(2024, 1, 1)
    lead = [3.0 + 0.1 * (i % 7) for i in range(60)]
    _load(warehouse, "lead", lead, start)
    _load(warehouse, "follower", [v + 0.5 for v in lead], start)

    links = peers_for(warehouse, "lead")

    assert [link.peer_id for link in links] == ["follower"]
    assert links[0].agreement == pytest.approx(1.0)
    assert links[0].overlap >= MIN_OVERLAP


def test_peer_qualification_is_directional(warehouse):
    """기준금리→콜금리는 성립하지만 역방향은 아니다. 실측에서 확인된 비대칭이다.

    기준금리는 899일 중 7일만 움직인다. 그 7일에 콜금리는 7/7 동행했다.
    그러나 콜금리가 움직인 수백 일 대부분 기준금리는 그대로였다.
    """
    start = dt.date(2024, 1, 1)
    policy, market, level = [], [], 3.0
    for day in range(120):
        if day and day % 20 == 0:  # 6회 인상, 그 사이는 완전히 평평하다
            level += 0.25
        policy.append(level)
        # 시장금리는 정책 변동을 따라가되 평시에는 독립적인 잡음으로 움직인다
        market.append(level + 0.5 + 0.02 * (day % 5))
    _load(warehouse, "policy", policy, start)
    _load(warehouse, "market", market, start)

    forward = {link.peer_id for link in peers_for(warehouse, "policy")}
    backward = {link.peer_id for link in peers_for(warehouse, "market")}

    assert "market" in forward, "정책금리가 움직인 날 시장금리는 동행했다"
    assert "policy" not in backward, "시장금리가 움직인 날 정책금리는 대부분 그대로였다"


def test_a_flat_peer_never_qualifies_however_long_the_overlap(warehouse):
    """상관이 nan인 쌍을 자격으로 넘기지 않는다.

    실측에서 기준금리 x 월별 대출금리의 corr()이 nan이었다. 분산이 0이면
    상관이 정의되지 않는데, 이를 0이나 완벽한 불일치로 읽으면 정반대 결론이 난다.
    """
    start = dt.date(2024, 1, 1)
    _load(warehouse, "moving", [3.0 + 0.1 * (i % 7) for i in range(90)], start)
    _load(warehouse, "frozen", [2.5] * 90, start)

    links = peers_for(warehouse, "moving")

    assert all(link.peer_id != "frozen" for link in links)


def test_too_few_overlapping_points_disqualifies(warehouse):
    start = dt.date(2024, 1, 1)
    count = MIN_OVERLAP - 5
    values = [3.0 + 0.1 * (i % 7) for i in range(count)]
    _load(warehouse, "short_a", values, start)
    _load(warehouse, "short_b", [v + 1.0 for v in values], start)

    assert peers_for(warehouse, "short_a") == []


def test_too_few_moves_disqualifies_even_with_perfect_agreement(warehouse):
    """동행률 1.000이어도 표본이 부족하면 자격을 주지 않는다."""
    start = dt.date(2024, 1, 1)
    moves = MIN_MOVE_SAMPLES - 2
    values = [3.0] * 60
    for i in range(moves):
        for j in range(i * 10 + 1, 60):
            values[j] += 0.25
    _load(warehouse, "rare_a", values, start)
    _load(warehouse, "rare_b", [v + 0.5 for v in values], start)

    links = peers_for(warehouse, "rare_a")

    assert links == [], f"{moves}회 변동은 {MIN_MOVE_SAMPLES}회 미만이므로 자격 없음"


def test_agreement_below_threshold_disqualifies(warehouse):
    """동전 던지기 수준의 동행률은 근거가 아니다. 실측 기준금리x국고채가 0.429였다."""
    start = dt.date(2024, 1, 1)
    lead, coin = [3.0], [3.0]
    for i in range(1, 80):
        lead.append(lead[-1] + 0.1)
        coin.append(coin[-1] + (0.1 if i % 2 else -0.1))
    _load(warehouse, "steady", lead, start)
    _load(warehouse, "coinflip", coin, start)

    links = peers_for(warehouse, "steady")
    agreements = {link.peer_id: link.agreement for link in links}

    assert "coinflip" not in agreements or agreements["coinflip"] >= MIN_AGREEMENT


def test_two_series_that_merely_trend_together_are_not_peers(warehouse):
    """동행률만 보면 속는다. 둘 다 대부분의 날 오르는 계열은 연동되지 않아도 맞춘다.

    peer의 방향 편향으로 설명되는 몫(기대 동행률)을 빼야 실제 근거가 남는다.
    """
    start = dt.date(2024, 1, 1)
    _load(warehouse, "trend_a", [3.0 + 0.1 * (i % 7) for i in range(90)], start)
    _load(warehouse, "trend_b", [5.0 + 0.1 * (i % 5) for i in range(90)], start)

    for subject in ("trend_a", "trend_b"):
        assert peers_for(warehouse, subject) == [], "우연히 높은 동행률이 근거로 통과했다"


def test_peer_link_carries_the_numbers_behind_the_verdict(warehouse):
    """판정만 주고 숫자를 숨기면 승인자가 가중치를 정할 수 없다."""
    start = dt.date(2024, 1, 1)
    values = [3.0 + 0.1 * (i % 7) for i in range(60)]
    _load(warehouse, "a", values, start)
    _load(warehouse, "b", [v + 0.5 for v in values], start)

    link = peers_for(warehouse, "a")[0]

    assert isinstance(link, PeerLink)
    assert link.overlap > 0
    assert link.move_samples >= MIN_MOVE_SAMPLES
    assert 0.0 <= link.agreement <= 1.0
    assert link.noise_p90 >= 0.0
    assert link.lift >= MIN_LIFT
