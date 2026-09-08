import datetime as dt

import pytest

from fingate.review.probes import Evidence, ProbeVerdict
from fingate.review.recommend import Recommendation, recommend

NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)


def _evidence(probe: str, verdict: ProbeVerdict) -> Evidence:
    return Evidence(probe, verdict, f"{probe}={verdict}", {})


def test_a_value_our_pipeline_invented_is_rejected_outright():
    """원본에 없는 값은 승인 대상이 아니라 버그다."""
    result = recommend(
        [
            _evidence("raw_provenance", ProbeVerdict.SUPPORTS_DEFECT),
            _evidence("peer_corroboration", ProbeVerdict.SUPPORTS_REAL),
        ]
    )

    assert result.verdict is Recommendation.REJECT


def test_raw_mismatch_outranks_peer_corroboration():
    """peer가 아무리 강하게 동행해도 우리 결함을 승인하지 않는다.

    우선순위가 뒤집히면 그럴듯한 결함이 통과한다. 가장 위험한 경우다.
    """
    result = recommend(
        [
            _evidence("peer_corroboration", ProbeVerdict.SUPPORTS_REAL),
            _evidence("raw_provenance", ProbeVerdict.SUPPORTS_DEFECT),
        ]
    )

    assert result.verdict is Recommendation.REJECT
    assert "raw_provenance" in result.because


def test_a_lone_spike_is_recommended_for_rejection():
    result = recommend(
        [
            _evidence("raw_provenance", ProbeVerdict.CONTEXT),
            _evidence("peer_corroboration", ProbeVerdict.SUPPORTS_DEFECT),
        ]
    )

    assert result.verdict is Recommendation.REJECT_LIKELY


def test_a_corroborated_move_is_recommended_for_approval():
    result = recommend(
        [
            _evidence("raw_provenance", ProbeVerdict.CONTEXT),
            _evidence("peer_corroboration", ProbeVerdict.SUPPORTS_REAL),
        ]
    )

    assert result.verdict is Recommendation.APPROVE_LIKELY


def test_no_evidence_yields_no_recommendation():
    """근거가 없을 때 권고를 지어내는 것이 근거 없이 결정하는 것보다 나쁘다."""
    result = recommend(
        [
            _evidence("raw_provenance", ProbeVerdict.INSUFFICIENT),
            _evidence("peer_corroboration", ProbeVerdict.INSUFFICIENT),
        ]
    )

    assert result.verdict is Recommendation.INSUFFICIENT_EVIDENCE


def test_context_probes_never_drive_the_recommendation():
    """맥락 probe는 결정에 방향을 주지 않는다."""
    result = recommend(
        [
            _evidence("historical_precedent", ProbeVerdict.CONTEXT),
            _evidence("blast_radius", ProbeVerdict.CONTEXT),
            _evidence("prior_decisions", ProbeVerdict.CONTEXT),
        ]
    )

    assert result.verdict is Recommendation.INSUFFICIENT_EVIDENCE


def test_the_recommendation_always_names_the_probe_it_rests_on():
    for verdict, expected in (
        (ProbeVerdict.SUPPORTS_REAL, Recommendation.APPROVE_LIKELY),
        (ProbeVerdict.SUPPORTS_DEFECT, Recommendation.REJECT_LIKELY),
    ):
        result = recommend([_evidence("peer_corroboration", verdict)])
        assert result.verdict is expected
        assert "peer_corroboration" in result.because


@pytest.mark.parametrize("value", list(Recommendation))
def test_no_recommendation_value_reads_as_an_executed_decision(value):
    """권고는 제안이다. 'approved'/'rejected'로 읽히는 값을 쓰지 않는다.

    원장의 상태값과 권고값이 같은 단어면 감사 로그에서 누가 결정했는지
    구분할 수 없게 된다.
    """
    assert str(value) not in ("approved", "rejected")


def test_conflicting_evidence_is_not_resolved_by_picking_a_side():
    """동행률과 스프레드가 갈리면 한쪽을 임의로 채택하지 않는다.

    둘은 다른 질문에 답한다. 갈린다는 사실 자체가 승인자에게 필요한 정보다.
    """
    result = recommend(
        [
            _evidence("peer_corroboration", ProbeVerdict.SUPPORTS_REAL),
            _evidence("anchor_spread", ProbeVerdict.SUPPORTS_DEFECT),
        ]
    )

    assert result.verdict is Recommendation.INSUFFICIENT_EVIDENCE
    assert "peer_corroboration" in result.because
    assert "anchor_spread" in result.because


def test_the_anchor_alone_can_carry_the_recommendation():
    """동행 peer 가 없는 계열(call_rate_daily)에서 스프레드가 사각지대를 메운다."""
    for verdict, expected in (
        (ProbeVerdict.SUPPORTS_DEFECT, Recommendation.REJECT_LIKELY),
        (ProbeVerdict.SUPPORTS_REAL, Recommendation.APPROVE_LIKELY),
    ):
        result = recommend(
            [
                _evidence("peer_corroboration", ProbeVerdict.INSUFFICIENT),
                _evidence("anchor_spread", verdict),
            ]
        )
        assert result.verdict is expected
        assert "anchor_spread" in result.because
