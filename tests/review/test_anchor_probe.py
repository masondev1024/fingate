import datetime as dt

import pytest

from fingate.collect.series import series_by_id
from fingate.contracts.schema import Observation
from fingate.review.probes import ProbeVerdict, anchor_spread
from fingate.warehouse.store import Warehouse

NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
START = dt.date(2024, 1, 1)
SPEC = series_by_id("call_rate_daily")
ANCHOR = "base_rate_daily"


def _load(warehouse, series_id, values):
    warehouse.load_observations(
        [
            Observation(
                series_id=series_id,
                period=START + dt.timedelta(days=i),
                raw_period=(START + dt.timedelta(days=i)).strftime("%Y%m%d"),
                value=v,
                unit="percent_per_annum",
                request_id="req",
            )
            for i, v in enumerate(values)
        ],
        NOW,
    )


def _pair(subject_jump: float, anchor_jump: float):
    """앵커에 붙은 계열. 마지막 날 각각 급변을 준다."""
    anchor, subject, level = [], [], 3.0
    for day in range(150):
        if day and day % 50 == 0:
            level += 0.25
        anchor.append(level)
        subject.append(level + 0.02 + 0.002 * ((day % 5) - 2))
    anchor.append(anchor[-1] + anchor_jump)
    subject.append(subject[-1] + subject_jump)
    return anchor, subject


@pytest.fixture
def warehouse():
    with Warehouse() as store:
        yield store


def _run(warehouse, subject_jump, anchor_jump):
    anchor, subject = _pair(subject_jump, anchor_jump)
    _load(warehouse, ANCHOR, anchor)
    _load(warehouse, SPEC.series_id, subject)
    suspect = START + dt.timedelta(days=len(subject) - 1)
    return anchor_spread(warehouse, SPEC, suspect)


def test_a_jump_the_anchor_followed_supports_a_real_change(warehouse):
    """앵커가 같이 움직여 스프레드가 유지되면 실제 변동이다."""
    evidence = _run(warehouse, subject_jump=0.9, anchor_jump=0.9)

    assert evidence.verdict is ProbeVerdict.SUPPORTS_REAL


def test_a_jump_that_breaks_the_level_relationship_supports_a_defect(warehouse):
    """T-18 의 가짜 승인이 정확히 이 형태였다. 콜금리만 0.9 튀고 기준금리는 그대로."""
    evidence = _run(warehouse, subject_jump=0.9, anchor_jump=0.0)

    assert evidence.verdict is ProbeVerdict.SUPPORTS_DEFECT
    assert evidence.facts["anchors"][0]["z"] > evidence.facts["anchors"][0]["alert_z"]


def test_a_series_with_no_anchor_is_insufficient_not_defect(warehouse):
    _load(warehouse, SPEC.series_id, [3.0 + 0.01 * (i % 7) for i in range(60)])
    suspect = START + dt.timedelta(days=59)

    evidence = anchor_spread(warehouse, SPEC, suspect)

    assert evidence.verdict is ProbeVerdict.INSUFFICIENT


def test_a_missing_observation_at_the_suspect_period_is_insufficient(warehouse):
    anchor, subject = _pair(0.9, 0.0)
    _load(warehouse, ANCHOR, anchor)
    _load(warehouse, SPEC.series_id, subject)

    evidence = anchor_spread(warehouse, SPEC, dt.date(2030, 1, 1))

    assert evidence.verdict is ProbeVerdict.INSUFFICIENT


def test_the_evidence_exposes_the_measured_numbers(warehouse):
    evidence = _run(warehouse, subject_jump=0.9, anchor_jump=0.0)
    entry = evidence.facts["anchors"][0]

    assert entry["anchor_id"] == ANCHOR
    assert entry["spread_sd"] > 0
    assert "z" in entry and "alert_z" in entry and "observed_max_z" in entry
