"""승인 CLI 테스트.

담당자가 차단된 건을 실제로 보고 결정할 수 있어야 한다. 이 인터페이스가
없으면 "사람이 승인한다"는 제품 서사에 실체가 없다.
"""

import datetime as dt

import pytest

from fingate.contracts.quality import QualityFinding
from fingate.gate.cli import main
from fingate.gate.ledger import ExceptionLedger, ExceptionStatus

NOW = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)
FINDING = QualityFinding(
    "base_rate_daily", "JUMP_EXCEEDED", "0.9 exceeds 0.75", dt.date(2026, 9, 4)
)


@pytest.fixture
def audit(tmp_path):
    path = tmp_path / "audit.jsonl"
    ledger = ExceptionLedger(path)
    ledger.stage("base_rate_daily", [FINDING], "req-1", NOW, dt.timedelta(days=2))
    return path


def _pending_id(audit) -> str:
    return ExceptionLedger(audit).list(status=ExceptionStatus.PENDING)[0].exception_id


def test_list_shows_pending_exceptions(audit, capsys):
    assert main(["--audit", str(audit), "list"]) == 0
    out = capsys.readouterr().out
    assert "base_rate_daily" in out
    assert "pending" in out


def test_list_reports_when_nothing_is_pending(tmp_path, capsys):
    assert main(["--audit", str(tmp_path / "empty.jsonl"), "list"]) == 0
    assert "없습니다" in capsys.readouterr().out


def test_show_displays_the_evidence(audit, capsys):
    """근거 없이는 승인할 수 없다. 무엇을 위반했는지 보여야 한다."""
    assert main(["--audit", str(audit), "show", _pending_id(audit)]) == 0
    out = capsys.readouterr().out
    assert "JUMP_EXCEEDED" in out
    assert "0.9 exceeds 0.75" in out
    assert "req-1" in out


def test_approve_records_the_decision(audit, capsys):
    code = main(
        [
            "--audit",
            str(audit),
            "approve",
            _pending_id(audit),
            "--by",
            "mason",
            "--note",
            "금통위 인상 확인",
        ]
    )
    assert code == 0
    approved = ExceptionLedger(audit).list(status=ExceptionStatus.APPROVED)
    assert len(approved) == 1
    assert approved[0].decided_by == "mason"
    assert approved[0].decision_note == "금통위 인상 확인"


def test_reject_records_the_decision(audit):
    main(
        [
            "--audit",
            str(audit),
            "reject",
            _pending_id(audit),
            "--by",
            "mason",
            "--note",
            "수집 오류로 판단",
        ]
    )
    rejected = ExceptionLedger(audit).list(status=ExceptionStatus.REJECTED)
    assert rejected[0].decision_note == "수집 오류로 판단"


def test_approve_requires_a_note(audit, capsys):
    code = main(
        [
            "--audit",
            str(audit),
            "approve",
            _pending_id(audit),
            "--by",
            "mason",
            "--note",
            "  ",
        ]
    )
    assert code == 1
    assert "note" in capsys.readouterr().err


def test_unknown_exception_id_exits_nonzero(audit, capsys):
    code = main(
        [
            "--audit",
            str(audit),
            "approve",
            "nope",
            "--by",
            "mason",
            "--note",
            "x",
        ]
    )
    assert code == 1
    assert "unknown exception" in capsys.readouterr().err


def test_expired_exception_cannot_be_approved(audit, capsys):
    code = main(
        [
            "--audit",
            str(audit),
            "approve",
            _pending_id(audit),
            "--by",
            "mason",
            "--note",
            "늦은 승인",
            "--now",
            "2026-09-30T00:00:00+00:00",
        ]
    )
    assert code == 1
    assert "expired" in capsys.readouterr().err


def test_decision_is_visible_to_a_later_process(audit, capsys):
    """CLI가 남긴 결정을 파이프라인이 읽을 수 있어야 한다."""
    main(
        [
            "--audit",
            str(audit),
            "approve",
            _pending_id(audit),
            "--by",
            "mason",
            "--note",
            "확인",
        ]
    )
    ledger = ExceptionLedger(audit)
    assert ledger.is_cleared("base_rate_daily", now=NOW) is True
