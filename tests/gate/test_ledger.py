"""승인 게이트 테스트.

품질 위반은 자동으로 서빙을 막는다. 그러나 정당한 급변(정책금리 인상,
회계기준 변경)이 있으므로 사람이 근거를 보고 통과시킬 경로를 둔다.
승인 없이는 통과하지 않고, 모든 결정은 감사 로그에 남는다.
"""

import datetime as dt
import json

import pytest

from fingate.contracts.quality import QualityFinding
from fingate.gate.ledger import ExceptionLedger, ExceptionStatus

NOW = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(hours=2)
AFTER_TTL = NOW + dt.timedelta(days=3)

FINDING = QualityFinding(
    series_id="base_rate_daily",
    rule="JUMP_EXCEEDED",
    detail="0.900 exceeds 0.75",
    period=dt.date(2026, 9, 4),
)


def _ledger(tmp_path) -> ExceptionLedger:
    return ExceptionLedger(audit_path=tmp_path / "audit.jsonl")


def _staged(ledger, **overrides):
    kwargs = dict(
        series_id="base_rate_daily",
        findings=[FINDING],
        request_id="req-1",
        now=NOW,
        ttl=dt.timedelta(days=2),
    )
    kwargs.update(overrides)
    return ledger.stage(**kwargs)


def test_staged_exception_starts_pending(tmp_path):
    staged = _staged(_ledger(tmp_path))
    assert staged.status is ExceptionStatus.PENDING
    assert staged.decided_at is None


def test_cannot_stage_without_findings(tmp_path):
    """근거 없는 예외는 만들 수 없다. 승인자가 판단할 대상이 없다."""
    with pytest.raises(ValueError, match="findings"):
        _staged(_ledger(tmp_path), findings=[])


def test_approval_requires_actor_and_note(tmp_path):
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    with pytest.raises(ValueError, match="decided_by"):
        ledger.approve(staged.exception_id, decided_by="  ", note="ok", now=LATER)
    with pytest.raises(ValueError, match="note"):
        ledger.approve(staged.exception_id, decided_by="mason", note="   ", now=LATER)


def test_approval_records_decision(tmp_path):
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    approved = ledger.approve(
        staged.exception_id, decided_by="mason", note="정책금리 인상 확인", now=LATER
    )
    assert approved.status is ExceptionStatus.APPROVED
    assert approved.decided_by == "mason"
    assert approved.decided_at == LATER


def test_rejection_requires_a_reason(tmp_path):
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    with pytest.raises(ValueError, match="note"):
        ledger.reject(staged.exception_id, decided_by="mason", note="", now=LATER)


def test_expired_exception_cannot_be_approved(tmp_path):
    """오래된 승인은 재사용할 수 없다. 근거가 된 상황이 이미 지났다."""
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    with pytest.raises(ValueError, match="expired"):
        ledger.approve(staged.exception_id, decided_by="mason", note="늦은 승인", now=AFTER_TTL)
    assert ledger.get(staged.exception_id).status is ExceptionStatus.EXPIRED


def test_already_decided_exception_cannot_be_decided_again(tmp_path):
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    ledger.approve(staged.exception_id, decided_by="mason", note="확인", now=LATER)
    with pytest.raises(ValueError, match="already decided"):
        ledger.reject(staged.exception_id, decided_by="mason", note="번복", now=LATER)


def test_unknown_exception_raises(tmp_path):
    with pytest.raises(KeyError, match="unknown exception"):
        _ledger(tmp_path).approve("nope", decided_by="mason", note="x", now=LATER)


def test_lists_by_status(tmp_path):
    ledger = _ledger(tmp_path)
    first = _staged(ledger)
    _staged(ledger, request_id="req-2")
    ledger.approve(first.exception_id, decided_by="mason", note="확인", now=LATER)
    assert len(ledger.list(status=ExceptionStatus.PENDING)) == 1
    assert len(ledger.list(status=ExceptionStatus.APPROVED)) == 1
    assert len(ledger.list()) == 2


def test_is_cleared_only_when_approved_and_unexpired(tmp_path):
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    assert ledger.is_cleared("base_rate_daily", now=LATER) is False
    ledger.approve(staged.exception_id, decided_by="mason", note="확인", now=LATER)
    assert ledger.is_cleared("base_rate_daily", now=LATER) is True
    assert ledger.is_cleared("base_rate_daily", now=AFTER_TTL) is False


def test_every_action_is_written_to_the_audit_log(tmp_path):
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    ledger.approve(staged.exception_id, decided_by="mason", note="정책금리 인상", now=LATER)
    lines = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["action"] for entry in lines] == ["staged", "approved"]
    assert lines[0]["request_id"] == "req-1"
    assert lines[0]["findings"][0]["rule"] == "JUMP_EXCEEDED"
    assert lines[1]["decided_by"] == "mason"
    assert lines[1]["note"] == "정책금리 인상"


def test_audit_log_records_expiry_too(tmp_path):
    ledger = _ledger(tmp_path)
    staged = _staged(ledger)
    with pytest.raises(ValueError):
        ledger.approve(staged.exception_id, decided_by="mason", note="늦음", now=AFTER_TTL)
    actions = [
        json.loads(line)["action"]
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert actions == ["staged", "expired"]
