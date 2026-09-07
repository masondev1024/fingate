import datetime as dt
import json

import pytest

from fingate.contracts.quality import QualityFinding
from fingate.contracts.schema import Observation
from fingate.gate.cli import main as gate_main
from fingate.gate.ledger import ExceptionLedger
from fingate.review.cli import main as review_main
from fingate.warehouse.store import Warehouse

NOW = "2026-09-08T00:00:00+00:00"
SERIES = "base_rate_daily"
PEER = "call_rate_daily"
START = dt.date(2026, 1, 1)


def _series(jump: float, peer_jump: float):
    subject, peer, level = [], [], 3.0
    for day in range(120):
        if day and day % 20 == 0:
            level += 0.25
        subject.append(level)
        peer.append(level + 0.5)
    subject.append(subject[-1] + jump)
    peer.append(peer[-1] + peer_jump)
    return subject, peer


@pytest.fixture
def bench(tmp_path):
    """실제 파일 경로 위에 차단된 건 하나를 만든다. CLI는 별도 프로세스처럼 읽는다."""
    subject, peer = _series(jump=0.85, peer_jump=0.80)
    suspect = START + dt.timedelta(days=len(subject) - 1)

    body = json.dumps(
        {
            "StatisticSearch": {
                "list_total_count": 1,
                "row": [{"TIME": suspect.strftime("%Y%m%d"), "DATA_VALUE": f"{subject[-1]}"}],
            }
        }
    )
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    from fingate.collect.raw_store import RawStore

    request_id = RawStore(raw_dir).put("ecos", "s", {}, body.encode("utf-8")).request_id

    warehouse_path = tmp_path / "fingate.duckdb"
    with Warehouse(warehouse_path) as warehouse:
        for series_id, values in ((SERIES, subject), (PEER, peer)):
            warehouse.load_observations(
                [
                    Observation(
                        series_id=series_id,
                        period=START + dt.timedelta(days=index),
                        raw_period=(START + dt.timedelta(days=index)).strftime("%Y%m%d"),
                        value=value,
                        unit="percent_per_annum",
                        request_id=request_id,
                    )
                    for index, value in enumerate(values)
                ],
                dt.datetime.fromisoformat(NOW),
            )

    audit = tmp_path / "audit.jsonl"
    finding = QualityFinding(SERIES, "JUMP_EXCEEDED", "0.85 exceeds 0.75", suspect)
    staged = ExceptionLedger(audit).stage(
        SERIES, [finding], request_id, dt.datetime.fromisoformat(NOW), dt.timedelta(days=7)
    )

    return {
        "id": staged.exception_id,
        "args": [
            "--audit",
            str(audit),
            "--warehouse",
            str(warehouse_path),
            "--raw",
            str(raw_dir),
            "--serving",
            str(tmp_path / "serving"),
        ],
        "audit": audit,
    }


def test_review_prints_every_probe_and_the_recommendation(bench, capsys):
    code = review_main([bench["id"], *bench["args"], "--now", NOW])
    out = capsys.readouterr().out

    assert code == 0
    for probe in (
        "peer_corroboration",
        "raw_provenance",
        "historical_precedent",
        "blast_radius",
        "prior_decisions",
    ):
        assert probe in out
    assert "approve_likely" in out


def test_review_states_that_it_cannot_decide(bench, capsys):
    """권고가 결정으로 오독되면 안 된다. 화면에 명시한다."""
    review_main([bench["id"], *bench["args"], "--now", NOW])
    out = capsys.readouterr().out

    assert "승인" in out and "사람" in out


def test_review_emits_machine_readable_output(bench, capsys):
    review_main([bench["id"], *bench["args"], "--now", NOW, "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["recommendation"]["verdict"] == "approve_likely"
    assert len(payload["evidence"]) == 5
    assert payload["exception_id"] == bench["id"]


def test_review_of_an_unknown_exception_fails_cleanly(bench, capsys):
    code = review_main(["does-not-exist", *bench["args"], "--now", NOW])

    assert code == 1
    assert "does-not-exist" in capsys.readouterr().err


def test_approving_records_the_recommendation_computed_at_decision_time(bench):
    """승인자의 자기 신고가 아니라 시스템이 계산한 값을 남긴다.

    자기 신고면 거수기 탐지가 무의미해진다.
    """
    code = gate_main(
        [
            "--audit",
            str(bench["audit"]),
            *[arg for arg in bench["args"] if arg != "--audit" and arg != str(bench["audit"])],
            "approve",
            bench["id"],
            "--by",
            "mason",
            "--note",
            "빅스텝",
            "--now",
            NOW,
        ]
    )

    assert code == 0
    decided = ExceptionLedger(bench["audit"]).get(bench["id"])
    assert decided.saw_recommendation == "approve_likely"


def test_approving_without_a_reachable_warehouse_still_works(tmp_path):
    """근거를 못 모아도 승인 자체는 막히지 않는다. 권고 없음으로 기록된다."""
    audit = tmp_path / "audit.jsonl"
    finding = QualityFinding(SERIES, "JUMP_EXCEEDED", "0.85 exceeds 0.75", None)
    staged = ExceptionLedger(audit).stage(
        SERIES, [finding], "req", dt.datetime.fromisoformat(NOW), dt.timedelta(days=7)
    )

    code = gate_main(
        [
            "--audit",
            str(audit),
            "--warehouse",
            str(tmp_path / "missing.duckdb"),
            "approve",
            staged.exception_id,
            "--by",
            "mason",
            "--note",
            "확인",
            "--now",
            NOW,
        ]
    )

    assert code == 0
    assert ExceptionLedger(audit).get(staged.exception_id).saw_recommendation == ""


def test_gate_audit_reports_concurrence(bench, capsys):
    gate_main(
        [
            "--audit",
            str(bench["audit"]),
            *[arg for arg in bench["args"] if arg != "--audit" and arg != str(bench["audit"])],
            "approve",
            bench["id"],
            "--by",
            "mason",
            "--note",
            "빅스텝",
            "--now",
            NOW,
        ]
    )
    capsys.readouterr()

    code = gate_main(["--audit", str(bench["audit"]), "audit"])
    out = capsys.readouterr().out

    assert code == 0
    assert "1" in out
    assert "표본" in out or "일치" in out
