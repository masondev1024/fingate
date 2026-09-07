"""품질 계약 테스트.

임계는 2015~2026 실측 분포에서 도출했다. 기준금리 빅스텝 0.50%p는
정상이므로 급변 임계는 그보다 위에 둔다.
"""

import datetime as dt

import pytest

from fingate.collect.series import series_by_id
from fingate.contracts.quality import check_quality
from fingate.contracts.schema import Observation

SPEC = series_by_id("base_rate_daily")


def _obs(day: int, value: float = 3.5) -> Observation:
    return Observation(
        series_id=SPEC.series_id,
        period=dt.date(2026, 9, day),
        raw_period=f"202609{day:02d}",
        value=value,
        unit=SPEC.unit,
        request_id="req-1",
    )


AS_OF = dt.date(2026, 9, 7)


def test_clean_series_has_no_findings():
    report = check_quality(SPEC, [_obs(3), _obs(4), _obs(7)], as_of=AS_OF)
    assert report.ok
    assert report.findings == []


def test_detects_duplicate_period():
    report = check_quality(SPEC, [_obs(3), _obs(3)], as_of=AS_OF)
    assert [f.rule for f in report.findings] == ["DUPLICATE_PERIOD"]
    assert not report.ok


def test_detects_value_below_range():
    report = check_quality(SPEC, [_obs(7, value=-99.0)], as_of=AS_OF)
    assert [f.rule for f in report.findings] == ["VALUE_OUT_OF_RANGE"]


def test_detects_value_above_range():
    report = check_quality(SPEC, [_obs(7, value=999.0)], as_of=AS_OF)
    assert [f.rule for f in report.findings] == ["VALUE_OUT_OF_RANGE"]


def test_accepts_policy_big_step():
    """기준금리 0.50%p 빅스텝은 실제로 있었던 정상 변동이다."""
    report = check_quality(SPEC, [_obs(3, 3.0), _obs(7, 3.5)], as_of=AS_OF)
    assert report.ok


def test_detects_jump_beyond_threshold():
    report = check_quality(SPEC, [_obs(3, 3.0), _obs(7, 5.0)], as_of=AS_OF)
    assert [f.rule for f in report.findings] == ["JUMP_EXCEEDED"]


def test_detects_stale_series():
    old = dt.date(2026, 8, 1)
    stale = Observation(SPEC.series_id, old, "20260801", 3.5, SPEC.unit, "req-1")
    report = check_quality(SPEC, [stale], as_of=AS_OF)
    assert [f.rule for f in report.findings] == ["STALE"]


def test_freshness_uses_declared_sla():
    """SLA 5일이므로 5일 전 관측은 통과하고 6일 전은 위반이다."""
    within = Observation(SPEC.series_id, dt.date(2026, 9, 2), "20260902", 3.5, SPEC.unit, "r")
    beyond = Observation(SPEC.series_id, dt.date(2026, 9, 1), "20260901", 3.5, SPEC.unit, "r")
    assert check_quality(SPEC, [within], as_of=AS_OF).ok
    assert not check_quality(SPEC, [beyond], as_of=AS_OF).ok


def test_empty_series_is_a_finding_not_a_pass():
    """관측이 하나도 없는 것을 통과로 처리하면 빈 서빙이 정상이 된다."""
    report = check_quality(SPEC, [], as_of=AS_OF)
    assert [f.rule for f in report.findings] == ["NO_OBSERVATIONS"]


def test_reports_every_violation_not_just_the_first():
    observations = [_obs(3, 3.0), _obs(3, 3.0), _obs(7, 999.0)]
    rules = {f.rule for f in check_quality(SPEC, observations, as_of=AS_OF).findings}
    assert "DUPLICATE_PERIOD" in rules
    assert "VALUE_OUT_OF_RANGE" in rules


def test_findings_carry_period_and_series():
    report = check_quality(SPEC, [_obs(7, 999.0)], as_of=AS_OF)
    finding = report.findings[0]
    assert finding.series_id == SPEC.series_id
    assert finding.period == dt.date(2026, 9, 7)


def test_rejects_observations_from_another_series():
    other = Observation("ktb_3y_daily", dt.date(2026, 9, 7), "20260907", 3.0, SPEC.unit, "r")
    with pytest.raises(ValueError, match="series"):
        check_quality(SPEC, [other], as_of=AS_OF)
