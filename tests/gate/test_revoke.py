import datetime as dt

import pytest

from fingate.contracts.quality import QualityFinding
from fingate.gate.ledger import ExceptionLedger, ExceptionStatus, concurrence

NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(hours=1)
TTL = dt.timedelta(days=7)
SERIES = "base_rate_daily"


def _finding() -> QualityFinding:
    return QualityFinding(SERIES, "JUMP_EXCEEDED", "0.9 exceeds 0.75", dt.date(2026, 9, 4))


@pytest.fixture
def ledger(tmp_path):
    return ExceptionLedger(tmp_path / "audit.jsonl")


def _approved(ledger, recommendation: str = ""):
    staged = ledger.stage(SERIES, [_finding()], "req", NOW, TTL)
    ledger.approve(
        staged.exception_id,
        decided_by="mason",
        note="금통위 인상 확인",
        now=NOW,
        recommendation=recommendation,
    )
    return staged.exception_id


def test_an_approval_can_be_withdrawn(ledger):
    """설계는 '되돌리려면 사람이 개입해야 한다'고 적었지만 경로가 없었다."""
    exception_id = _approved(ledger)

    revoked = ledger.revoke(exception_id, decided_by="mason", note="근거 없음 확인", now=LATER)

    assert revoked.status is ExceptionStatus.REVOKED


def test_a_revoked_approval_stops_clearing_the_series(ledger):
    """철회의 요점이다. 철회해도 계속 통과하면 아무 의미가 없다."""
    exception_id = _approved(ledger)
    assert ledger.is_cleared(SERIES, NOW)

    ledger.revoke(exception_id, decided_by="mason", note="근거 없음", now=LATER)

    assert not ledger.is_cleared(SERIES, LATER)


def test_revoking_records_who_and_why(ledger):
    exception_id = _approved(ledger)

    revoked = ledger.revoke(exception_id, decided_by="mason", note="원본 응답 없음", now=LATER)

    assert revoked.revoked_by == "mason"
    assert revoked.revocation_note == "원본 응답 없음"
    assert revoked.revoked_at == LATER


def test_a_revocation_without_a_reason_is_refused(ledger):
    """사유 없는 철회는 사유 없는 승인과 똑같이 추적 불가능하다."""
    exception_id = _approved(ledger)

    for by, note in (("", "사유"), ("mason", "   ")):
        with pytest.raises(ValueError):
            ledger.revoke(exception_id, decided_by=by, note=note, now=LATER)


def test_a_pending_exception_cannot_be_revoked(ledger):
    """대기 중인 건은 반려하는 것이지 철회하는 것이 아니다."""
    staged = ledger.stage(SERIES, [_finding()], "req", NOW, TTL)

    with pytest.raises(ValueError):
        ledger.revoke(staged.exception_id, decided_by="mason", note="사유", now=LATER)


def test_a_revocation_cannot_be_revoked_twice(ledger):
    exception_id = _approved(ledger)
    ledger.revoke(exception_id, decided_by="mason", note="사유", now=LATER)

    with pytest.raises(ValueError):
        ledger.revoke(exception_id, decided_by="mason", note="또", now=LATER)


def test_a_rejection_can_also_be_withdrawn(ledger):
    """반려도 틀릴 수 있다. 되돌릴 경로가 승인에만 있으면 비대칭이다."""
    staged = ledger.stage(SERIES, [_finding()], "req", NOW, TTL)
    ledger.reject(staged.exception_id, decided_by="mason", note="오류로 판단", now=NOW)

    revoked = ledger.revoke(staged.exception_id, decided_by="mason", note="재검토", now=LATER)

    assert revoked.status is ExceptionStatus.REVOKED


def test_the_revocation_survives_a_restart(ledger, tmp_path):
    """감사 로그가 진실의 출처다. 철회도 재생으로 복원되어야 한다."""
    exception_id = _approved(ledger)
    ledger.revoke(exception_id, decided_by="mason", note="근거 없음", now=LATER)

    reopened = ExceptionLedger(tmp_path / "audit.jsonl")
    restored = reopened.get(exception_id)

    assert restored.status is ExceptionStatus.REVOKED
    assert restored.revocation_note == "근거 없음"
    assert not reopened.is_cleared(SERIES, LATER)


def test_the_original_decision_is_preserved_not_erased(ledger):
    """철회는 기록을 지우는 것이 아니다. 누가 승인했었는지가 남아야 감사가 된다."""
    exception_id = _approved(ledger)

    revoked = ledger.revoke(exception_id, decided_by="auditor", note="근거 없음", now=LATER)

    assert revoked.decided_by == "mason"
    assert revoked.decision_note == "금통위 인상 확인"
    assert revoked.decided_at == NOW


def test_a_withdrawn_decision_leaves_the_concurrence_sample(ledger):
    """철회된 결정은 승인자의 판단력에 대한 근거로 쓰지 않는다."""
    exception_id = _approved(ledger, recommendation="approve_likely")
    assert concurrence(ledger).decided_with_recommendation == 1

    ledger.revoke(exception_id, decided_by="mason", note="근거 없음", now=LATER)

    assert concurrence(ledger).decided_with_recommendation == 0


def test_the_cli_can_withdraw_a_decision(tmp_path, capsys):
    from fingate.gate.cli import main as gate_main

    audit = tmp_path / "audit.jsonl"
    ledger = ExceptionLedger(audit)
    staged = ledger.stage(SERIES, [_finding()], "req", NOW, TTL)
    ledger.approve(staged.exception_id, decided_by="mason", note="확인", now=NOW)

    code = gate_main(
        [
            "--audit",
            str(audit),
            "--warehouse",
            str(tmp_path / "missing.duckdb"),
            "revoke",
            staged.exception_id,
            "--by",
            "auditor",
            "--note",
            "원본 응답 없음",
            "--now",
            LATER.isoformat(),
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "revoked" in out
    assert "mason" in out, "원래 결정 주체가 화면에 남아야 한다"
    assert "스냅샷" in out, "되돌리지 않는 것을 명시해야 한다"
    assert ExceptionLedger(audit).get(staged.exception_id).status is ExceptionStatus.REVOKED


def test_the_cli_refuses_to_withdraw_a_pending_exception(tmp_path, capsys):
    from fingate.gate.cli import main as gate_main

    audit = tmp_path / "audit.jsonl"
    staged = ExceptionLedger(audit).stage(SERIES, [_finding()], "req", NOW, TTL)

    code = gate_main(
        [
            "--audit",
            str(audit),
            "--warehouse",
            str(tmp_path / "missing.duckdb"),
            "revoke",
            staged.exception_id,
            "--by",
            "a",
            "--note",
            "b",
        ]
    )

    assert code == 1
    assert "pending" in capsys.readouterr().err


def test_the_audit_view_surfaces_withdrawals(tmp_path, capsys):
    from fingate.gate.cli import main as gate_main

    audit = tmp_path / "audit.jsonl"
    ledger = ExceptionLedger(audit)
    exception_id = _approved(ledger, recommendation="approve_likely")
    ledger.revoke(exception_id, decided_by="auditor", note="근거 없음", now=LATER)

    gate_main(["--audit", str(audit), "audit"])
    out = capsys.readouterr().out

    assert "철회된 결정" in out
