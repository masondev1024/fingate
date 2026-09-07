"""서빙 판정 테스트.

신선도 위반 시 낡은 값을 조용히 내보내지 않는다. last-known-good을
유지하되 degraded 상태와 마지막 정상 시각을 함께 노출한다. 조용히
내보내면 소비자가 그것을 최신으로 오인한다.
"""

import datetime as dt

import pytest

from fingate.collect.series import series_by_id
from fingate.contracts.quality import QualityFinding, QualityReport
from fingate.contracts.schema import Observation
from fingate.gate.ledger import ExceptionLedger
from fingate.serve.snapshot import ServingState, ServingStore, decide_serving

SPEC = series_by_id("base_rate_daily")
NOW = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)


def _obs(day: int, value: float = 3.5) -> Observation:
    return Observation(
        SPEC.series_id, dt.date(2026, 9, day), f"202609{day:02d}", value, SPEC.unit, "req-1"
    )


def _report(findings: list[QualityFinding], count: int = 3) -> QualityReport:
    return QualityReport(SPEC.series_id, findings, count, dt.date(2026, 9, 4))


FINDING = QualityFinding(SPEC.series_id, "JUMP_EXCEEDED", "0.9 exceeds 0.75", dt.date(2026, 9, 4))


def _store(tmp_path) -> ServingStore:
    return ServingStore(tmp_path / "serving")


def _ledger(tmp_path) -> ExceptionLedger:
    return ExceptionLedger(tmp_path / "audit.jsonl")


def test_clean_report_is_served(tmp_path):
    decision = decide_serving(
        SPEC, _report([]), [_obs(3), _obs(4)], _store(tmp_path), _ledger(tmp_path), NOW
    )
    assert decision.state is ServingState.FRESH
    assert len(decision.observations) == 2
    assert decision.findings == []


def test_violation_without_approval_is_blocked(tmp_path):
    decision = decide_serving(
        SPEC, _report([FINDING]), [_obs(3)], _store(tmp_path), _ledger(tmp_path), NOW
    )
    assert decision.state is ServingState.BLOCKED
    assert decision.observations == []


def test_first_violation_has_no_last_known_good(tmp_path):
    """이전 정상 스냅샷이 없으면 내보낼 것이 아예 없다."""
    decision = decide_serving(
        SPEC, _report([FINDING]), [_obs(3)], _store(tmp_path), _ledger(tmp_path), NOW
    )
    assert decision.state is ServingState.BLOCKED
    assert decision.last_good_at is None


def test_violation_falls_back_to_last_known_good_as_degraded(tmp_path):
    store, ledger = _store(tmp_path), _ledger(tmp_path)
    decide_serving(SPEC, _report([]), [_obs(3), _obs(4)], store, ledger, NOW)

    later = NOW + dt.timedelta(days=1)
    decision = decide_serving(SPEC, _report([FINDING]), [_obs(5, 9.9)], store, ledger, later)
    assert decision.state is ServingState.DEGRADED
    assert [o.value for o in decision.observations] == [3.5, 3.5]
    assert decision.last_good_at == NOW
    assert decision.findings == [FINDING]


def test_degraded_never_serves_the_violating_values(tmp_path):
    store, ledger = _store(tmp_path), _ledger(tmp_path)
    decide_serving(SPEC, _report([]), [_obs(3)], store, ledger, NOW)
    decision = decide_serving(
        SPEC, _report([FINDING]), [_obs(5, 9.9)], store, ledger, NOW + dt.timedelta(days=1)
    )
    assert all(o.value != 9.9 for o in decision.observations)


def test_approved_exception_lets_the_data_through(tmp_path):
    store, ledger = _store(tmp_path), _ledger(tmp_path)
    staged = ledger.stage(SPEC.series_id, [FINDING], "req-1", NOW, dt.timedelta(days=2))
    ledger.approve(staged.exception_id, decided_by="mason", note="정책금리 인상", now=NOW)

    decision = decide_serving(SPEC, _report([FINDING]), [_obs(5, 4.4)], store, ledger, NOW)
    assert decision.state is ServingState.APPROVED_OVERRIDE
    assert [o.value for o in decision.observations] == [4.4]


def test_expired_approval_does_not_let_data_through(tmp_path):
    store, ledger = _store(tmp_path), _ledger(tmp_path)
    staged = ledger.stage(SPEC.series_id, [FINDING], "req-1", NOW, dt.timedelta(days=2))
    ledger.approve(staged.exception_id, decided_by="mason", note="확인", now=NOW)

    much_later = NOW + dt.timedelta(days=5)
    decision = decide_serving(SPEC, _report([FINDING]), [_obs(5, 4.4)], store, ledger, much_later)
    assert decision.state is not ServingState.APPROVED_OVERRIDE


def test_snapshot_records_lineage(tmp_path):
    store = _store(tmp_path)
    decide_serving(SPEC, _report([]), [_obs(3)], store, _ledger(tmp_path), NOW)
    snapshot = store.load(SPEC.series_id)
    assert snapshot.request_ids == ("req-1",)
    assert snapshot.written_at == NOW


def test_snapshot_survives_a_new_store_instance(tmp_path):
    decide_serving(SPEC, _report([]), [_obs(3)], _store(tmp_path), _ledger(tmp_path), NOW)
    reopened = ServingStore(tmp_path / "serving")
    assert reopened.load(SPEC.series_id) is not None


def test_load_returns_none_for_unknown_series(tmp_path):
    assert _store(tmp_path).load("nope") is None


def test_empty_observations_are_never_promoted_to_snapshot(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="observations"):
        decide_serving(SPEC, _report([], count=0), [], store, _ledger(tmp_path), NOW)


def test_approved_data_becomes_the_new_baseline(tmp_path):
    """승인된 값은 정상 스냅샷으로 승격된다. 의도된 동작이며 명시적으로 고정한다.

    승인이 만료된 뒤 새 위반이 오면 degraded로 내보내는 last-known-good에
    그때 승인했던 값이 들어 있다. 승인은 "이 값은 정당하다"는 판단이고
    만료는 과거 판단의 소급 무효화가 아니다.
    """
    store, ledger = _store(tmp_path), _ledger(tmp_path)
    staged = ledger.stage(SPEC.series_id, [FINDING], "req-1", NOW, dt.timedelta(days=2))
    ledger.approve(staged.exception_id, decided_by="mason", note="정책금리 인상", now=NOW)

    approved = decide_serving(SPEC, _report([FINDING]), [_obs(5, 4.4)], store, ledger, NOW)
    assert approved.state is ServingState.APPROVED_OVERRIDE

    much_later = NOW + dt.timedelta(days=5)
    after = decide_serving(SPEC, _report([FINDING]), [_obs(6, 9.9)], store, ledger, much_later)
    assert after.state is ServingState.DEGRADED
    assert [o.value for o in after.observations] == [4.4]
    assert all(o.value != 9.9 for o in after.observations)
