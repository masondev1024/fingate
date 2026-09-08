import datetime as dt

import pytest

from fingate.contracts.schema import Observation
from fingate.review.anchors import (
    MIN_ANCHOR_OVERLAP,
    Z_FLOOR,
    AnchorLink,
    anchors_for,
)
from fingate.warehouse.store import Warehouse

LOADED_AT = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
START = dt.date(2024, 1, 1)


def _load(warehouse, series_id, values, start=START):
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


def _anchored(spread_noise: float, days: int = 120):
    """앵커에 붙어 있는 계열. 앵커가 거의 움직이지 않아도 관계는 유지된다."""
    anchor, follower, level = [], [], 3.0
    for day in range(days):
        if day and day % 40 == 0:
            level += 0.25
        anchor.append(level)
        follower.append(level + 0.05 + spread_noise * ((day % 5) - 2))
    return anchor, follower


def test_a_series_pinned_to_a_level_qualifies_even_when_the_anchor_rarely_moves(warehouse):
    """동행률은 앵커가 움직여야 성립하지만 스프레드는 관계가 유지되기만 하면 된다.

    실측에서 기준금리는 899일 중 892일 평평했다. 그래서 콜금리에는 자격
    peer 가 하나도 없었지만, 스프레드는 표준편차 0.0593 으로 극도로 안정적이었다.
    """
    anchor, follower = _anchored(spread_noise=0.002)
    _load(warehouse, "anchor", anchor)
    _load(warehouse, "follower", follower)

    links = anchors_for(warehouse, "follower", max_jump=1.0)

    assert [link.anchor_id for link in links] == ["anchor"]
    assert links[0].spread_sd < 0.05


def test_a_loose_relationship_cannot_detect_a_contract_sized_jump(warehouse):
    """스프레드가 흔들리는 만큼 급변이 묻힌다. 검출 못 하는 앵커는 근거가 아니다.

    실측 기준금리 x 국고채 스프레드 표준편차가 0.5041 이었다. 급변 임계 0.75 는
    그 안에 묻힌다.
    """
    anchor, follower = _anchored(spread_noise=0.30)
    _load(warehouse, "anchor", anchor)
    _load(warehouse, "follower", follower)

    assert anchors_for(warehouse, "follower", max_jump=0.75) == []


def test_too_few_overlapping_points_disqualifies(warehouse):
    anchor, follower = _anchored(spread_noise=0.002, days=MIN_ANCHOR_OVERLAP - 5)
    _load(warehouse, "anchor", anchor)
    _load(warehouse, "follower", follower)

    assert anchors_for(warehouse, "follower", max_jump=1.0) == []


def test_a_constant_spread_does_not_divide_by_zero(warehouse):
    """표준편차 0 이면 z 점수가 정의되지 않는다. nan 을 근거로 흘리지 않는다."""
    anchor, follower = _anchored(spread_noise=0.0)
    _load(warehouse, "anchor", anchor)
    _load(warehouse, "follower", follower)

    links = anchors_for(warehouse, "follower", max_jump=1.0)

    assert links == [] or all(link.spread_sd > 0 for link in links)


def test_the_alert_threshold_sits_above_what_normal_data_reached(warehouse):
    """교과서 3σ 를 쓰면 정상 데이터를 막는다.

    실측 콜금리 x 기준금리 스프레드는 꼬리가 두꺼워 정상 데이터에서 최대
    6.51σ 까지 벌어졌다. 3σ 임계는 900일 중 11일(1.22%)을 오탐한다.
    """
    anchor, follower = _anchored(spread_noise=0.002)
    follower[60] += 0.02  # 정상 범위 안의 이례적인 하루
    _load(warehouse, "anchor", anchor)
    _load(warehouse, "follower", follower)

    link = anchors_for(warehouse, "follower", max_jump=1.0)[0]

    assert link.alert_z >= Z_FLOOR
    assert link.alert_z > link.observed_max_z, "관측된 최대치보다 위에 있어야 한다"


def test_the_relationship_is_symmetric_unlike_co_movement(warehouse):
    """동행률 자격은 방향이 있었지만 스프레드는 대칭이다.

    실측에서 콜금리 -> 기준금리 동행률은 0.012 로 탈락했지만,
    스프레드 표준편차는 양방향 모두 0.0593 으로 같다.
    """
    anchor, follower = _anchored(spread_noise=0.002)
    _load(warehouse, "anchor", anchor)
    _load(warehouse, "follower", follower)

    forward = anchors_for(warehouse, "follower", max_jump=1.0)
    backward = anchors_for(warehouse, "anchor", max_jump=1.0)

    assert forward and backward
    assert forward[0].spread_sd == pytest.approx(backward[0].spread_sd)


def test_the_link_carries_the_numbers_behind_the_verdict(warehouse):
    anchor, follower = _anchored(spread_noise=0.002)
    _load(warehouse, "anchor", anchor)
    _load(warehouse, "follower", follower)

    link = anchors_for(warehouse, "follower", max_jump=1.0)[0]

    assert isinstance(link, AnchorLink)
    assert link.overlap >= MIN_ANCHOR_OVERLAP
    assert link.spread_sd > 0
    assert link.detectable_z > link.alert_z, "급변이 임계를 넘겨야 검출된다"
    assert "표준편차" in link.description or "σ" in link.description
