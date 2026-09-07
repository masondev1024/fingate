import datetime as dt

import pytest

from fingate.contracts.quality import QualityFinding
from fingate.gate.ledger import ExceptionLedger, concurrence

NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
TTL = dt.timedelta(days=7)


def _finding(series_id: str = "base_rate_daily") -> QualityFinding:
    return QualityFinding(series_id, "JUMP_EXCEEDED", "0.85 exceeds 0.75", None)


@pytest.fixture
def ledger(tmp_path):
    return ExceptionLedger(tmp_path / "audit.jsonl")


def test_the_recommendation_the_approver_saw_is_recorded(ledger):
    staged = ledger.stage("base_rate_daily", [_finding()], "req", NOW, TTL)

    decided = ledger.approve(
        staged.exception_id,
        decided_by="mason",
        note="빅스텝 인상",
        now=NOW,
        recommendation="approve_likely",
    )

    assert decided.saw_recommendation == "approve_likely"


def test_the_recorded_recommendation_survives_a_restart(ledger, tmp_path):
    staged = ledger.stage("base_rate_daily", [_finding()], "req", NOW, TTL)
    ledger.approve(
        staged.exception_id,
        decided_by="mason",
        note="확인",
        now=NOW,
        recommendation="reject_likely",
    )

    reopened = ExceptionLedger(tmp_path / "audit.jsonl")

    assert reopened.get(staged.exception_id).saw_recommendation == "reject_likely"


def test_deciding_without_a_recommendation_is_allowed(ledger):
    """권고를 보지 않고 결정할 수도 있다. 그 사실 자체가 기록되어야 한다."""
    staged = ledger.stage("base_rate_daily", [_finding()], "req", NOW, TTL)

    decided = ledger.approve(staged.exception_id, decided_by="mason", note="확인", now=NOW)

    assert decided.saw_recommendation == ""


def test_concurrence_counts_agreement_between_agent_and_human(ledger):
    for index, (recommendation, approve) in enumerate(
        [("approve_likely", True), ("reject_likely", False), ("approve_likely", False)]
    ):
        staged = ledger.stage(f"series_{index}", [_finding(f"series_{index}")], "req", NOW, TTL)
        decide = ledger.approve if approve else ledger.reject
        decide(
            staged.exception_id,
            decided_by="mason",
            note="확인",
            now=NOW,
            recommendation=recommendation,
        )

    report = concurrence(ledger)

    assert report.decided_with_recommendation == 3
    assert report.agreed == 2
    assert report.disagreed == 1


def test_concurrence_ignores_decisions_made_without_a_recommendation(ledger):
    staged = ledger.stage("base_rate_daily", [_finding()], "req", NOW, TTL)
    ledger.approve(staged.exception_id, decided_by="mason", note="확인", now=NOW)

    report = concurrence(ledger)

    assert report.decided_with_recommendation == 0
    assert report.rate is None


def test_total_agreement_is_flagged_as_a_rubber_stamp_risk(ledger):
    """사람이 에이전트와 100% 일치하면 판단하고 있지 않다는 신호다.

    거수기를 탐지할 수 없는 승인 절차는 승인 절차가 아니다.
    """
    for index in range(5):
        staged = ledger.stage(f"series_{index}", [_finding(f"series_{index}")], "req", NOW, TTL)
        ledger.approve(
            staged.exception_id,
            decided_by="mason",
            note="확인",
            now=NOW,
            recommendation="approve_likely",
        )

    report = concurrence(ledger)

    assert report.rate == pytest.approx(1.0)
    assert report.rubber_stamp_risk is True


def test_a_small_sample_is_not_called_a_rubber_stamp(ledger):
    """두 건 일치는 거수기가 아니라 표본 부족이다."""
    for index in range(2):
        staged = ledger.stage(f"series_{index}", [_finding(f"series_{index}")], "req", NOW, TTL)
        ledger.approve(
            staged.exception_id,
            decided_by="mason",
            note="확인",
            now=NOW,
            recommendation="approve_likely",
        )

    report = concurrence(ledger)

    assert report.rate == pytest.approx(1.0)
    assert report.rubber_stamp_risk is False
