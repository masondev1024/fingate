import datetime as dt
import json

import pytest

from fingate.collect.raw_store import RawStore
from fingate.collect.series import series_by_id
from fingate.contracts.quality import QualityFinding
from fingate.contracts.schema import Observation
from fingate.gate.ledger import ExceptionLedger
from fingate.review.probes import (
    Evidence,
    ProbeVerdict,
    blast_radius,
    historical_precedent,
    peer_corroboration,
    prior_decisions,
    raw_provenance,
)
from fingate.serve.snapshot import ServingStore, Snapshot
from fingate.warehouse.store import Warehouse

NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
SPEC = series_by_id("base_rate_daily")


def _load(warehouse, series_id, values, start):
    warehouse.load_observations(
        [
            Observation(
                series_id=series_id,
                period=start + dt.timedelta(days=index),
                raw_period=(start + dt.timedelta(days=index)).strftime("%Y%m%d"),
                value=value,
                unit="percent_per_annum",
                request_id="req-1",
            )
            for index, value in enumerate(values)
        ],
        NOW,
    )


@pytest.fixture
def warehouse():
    with Warehouse() as store:
        yield store


def _co_moving(jump: float, peer_jump: float) -> tuple[list[float], list[float]]:
    """의심 대상과 자격을 갖춘 peer를 만든다. 마지막 날 급변을 준다."""
    subject, peer, level = [], [], 3.0
    for day in range(120):
        if day and day % 20 == 0:
            level += 0.25
        subject.append(level)
        peer.append(level + 0.5)
    subject.append(subject[-1] + jump)
    peer.append(peer[-1] + peer_jump)
    return subject, peer


# --- peer_corroboration ---------------------------------------------------


def test_a_peer_that_moves_the_same_way_supports_a_real_change(warehouse):
    start = dt.date(2024, 1, 1)
    subject, peer = _co_moving(jump=0.85, peer_jump=0.80)
    _load(warehouse, "subject", subject, start)
    _load(warehouse, "peer", peer, start)
    suspect = start + dt.timedelta(days=len(subject) - 1)

    evidence = peer_corroboration(warehouse, "subject", suspect)

    assert evidence.verdict is ProbeVerdict.SUPPORTS_REAL
    assert "peer" in evidence.summary


def test_a_peer_that_stays_flat_supports_a_defect(warehouse):
    """연동된 계열이 꿈쩍도 않는데 한 계열만 튀면 그 계열의 결함이다."""
    start = dt.date(2024, 1, 1)
    subject, peer = _co_moving(jump=0.85, peer_jump=0.0)
    _load(warehouse, "subject", subject, start)
    _load(warehouse, "peer", peer, start)
    suspect = start + dt.timedelta(days=len(subject) - 1)

    evidence = peer_corroboration(warehouse, "subject", suspect)

    assert evidence.verdict is ProbeVerdict.SUPPORTS_DEFECT


def test_no_qualified_peer_is_insufficient_not_defect(warehouse):
    """근거가 없는 것을 결함의 증거로 쓰지 않는다.

    실측에서 기준금리 x 월별 대출금리의 동행률이 0.000이었다. 이는 '완벽히
    반대로 움직인다'가 아니라 '판단할 수 없다'였다. 그 구분을 잃으면 안 된다.
    """
    start = dt.date(2024, 1, 1)
    _load(warehouse, "lonely", [3.0 + 0.1 * (i % 7) for i in range(60)] + [9.0], start)
    suspect = start + dt.timedelta(days=60)

    evidence = peer_corroboration(warehouse, "lonely", suspect)

    assert evidence.verdict is ProbeVerdict.INSUFFICIENT


def test_peer_evidence_exposes_the_measured_numbers(warehouse):
    start = dt.date(2024, 1, 1)
    subject, peer = _co_moving(jump=0.85, peer_jump=0.80)
    _load(warehouse, "subject", subject, start)
    _load(warehouse, "peer", peer, start)
    suspect = start + dt.timedelta(days=len(subject) - 1)

    evidence = peer_corroboration(warehouse, "subject", suspect)

    assert isinstance(evidence, Evidence)
    assert evidence.facts["subject_change"] == pytest.approx(0.85)
    assert evidence.facts["peers"][0]["peer_id"] == "peer"
    assert evidence.facts["peers"][0]["agreement"] > 0.7


# --- raw_provenance -------------------------------------------------------


def _put_ecos(raw_store: RawStore, rows: list[dict]) -> str:
    body = json.dumps({"StatisticSearch": {"list_total_count": len(rows), "row": rows}})
    record = raw_store.put("ecos", "StatisticSearch", {"api_key": "s3cret"}, body.encode("utf-8"))
    return record.request_id


def test_a_value_present_in_the_raw_response_is_context_not_proof(tmp_path):
    """출처가 그렇게 보냈다는 사실은 그 값이 현실이라는 증명이 아니다."""
    store = RawStore(tmp_path)
    request_id = _put_ecos(store, [{"TIME": "20260827", "DATA_VALUE": "3.750"}])

    evidence = raw_provenance(store, SPEC, dt.date(2026, 8, 27), 3.75, request_id)

    assert evidence.verdict is ProbeVerdict.CONTEXT


def test_a_value_absent_from_the_raw_response_means_we_made_it_up(tmp_path):
    """원본과 다르면 출처 문제가 아니라 우리 파이프라인의 버그다."""
    store = RawStore(tmp_path)
    request_id = _put_ecos(store, [{"TIME": "20260827", "DATA_VALUE": "3.250"}])

    evidence = raw_provenance(store, SPEC, dt.date(2026, 8, 27), 3.75, request_id)

    assert evidence.verdict is ProbeVerdict.SUPPORTS_DEFECT
    assert evidence.facts["raw_value"] == pytest.approx(3.25)


def test_a_period_missing_from_the_raw_response_is_a_defect(tmp_path):
    store = RawStore(tmp_path)
    request_id = _put_ecos(store, [{"TIME": "20260826", "DATA_VALUE": "3.500"}])

    evidence = raw_provenance(store, SPEC, dt.date(2026, 8, 27), 3.75, request_id)

    assert evidence.verdict is ProbeVerdict.SUPPORTS_DEFECT


def test_a_missing_raw_record_is_insufficient(tmp_path):
    evidence = raw_provenance(RawStore(tmp_path), SPEC, dt.date(2026, 8, 27), 3.75, "gone")

    assert evidence.verdict is ProbeVerdict.INSUFFICIENT


def test_raw_provenance_never_leaks_credentials(tmp_path):
    store = RawStore(tmp_path)
    request_id = _put_ecos(store, [{"TIME": "20260827", "DATA_VALUE": "3.250"}])

    evidence = raw_provenance(store, SPEC, dt.date(2026, 8, 27), 3.75, request_id)

    assert "s3cret" not in json.dumps(evidence.facts) + evidence.summary


# --- historical_precedent -------------------------------------------------


def test_precedent_counts_earlier_breaches_of_the_same_threshold(warehouse):
    start = dt.date(2024, 1, 1)
    values = [3.0, 3.0, 4.0, 4.0, 5.2, 5.2, 5.3]
    _load(warehouse, SPEC.series_id, values, start)
    finding = QualityFinding(SPEC.series_id, "JUMP_EXCEEDED", "1.2 exceeds 0.75", start)

    evidence = historical_precedent(warehouse, SPEC, finding)

    assert evidence.facts["breach_count"] == 2
    assert evidence.facts["max_observed_jump"] == pytest.approx(1.2)


def test_no_precedent_is_reported_as_unprecedented(warehouse):
    start = dt.date(2024, 1, 1)
    _load(warehouse, SPEC.series_id, [3.0, 3.05, 3.1, 3.15], start)
    finding = QualityFinding(SPEC.series_id, "JUMP_EXCEEDED", "0.9 exceeds 0.75", start)

    evidence = historical_precedent(warehouse, SPEC, finding)

    assert evidence.facts["breach_count"] == 0


# --- blast_radius ---------------------------------------------------------


def test_blast_radius_reports_the_cost_of_rejecting(warehouse, tmp_path):
    serving = ServingStore(tmp_path)
    serving.save(
        Snapshot(
            series_id=SPEC.series_id,
            observations=(
                Observation(SPEC.series_id, dt.date(2026, 8, 1), "20260801", 3.5, "u", "r"),
            ),
            request_ids=("r",),
            written_at=NOW - dt.timedelta(days=38),
        )
    )

    evidence = blast_radius(warehouse, serving, SPEC.series_id, NOW)

    assert evidence.verdict is ProbeVerdict.CONTEXT
    assert evidence.facts["last_good_age_days"] == 38


def test_blast_radius_flags_a_series_with_no_fallback(warehouse, tmp_path):
    evidence = blast_radius(warehouse, ServingStore(tmp_path), SPEC.series_id, NOW)

    assert evidence.facts["last_good_age_days"] is None
    assert "없" in evidence.summary


# --- prior_decisions ------------------------------------------------------


def test_prior_decisions_surface_how_the_same_case_was_handled(tmp_path):
    ledger = ExceptionLedger(tmp_path / "audit.jsonl")
    finding = QualityFinding(SPEC.series_id, "JUMP_EXCEEDED", "0.85 exceeds 0.75", None)
    staged = ledger.stage(SPEC.series_id, [finding], "req", NOW, dt.timedelta(days=7))
    ledger.approve(staged.exception_id, decided_by="mason", note="빅스텝 인상", now=NOW)

    evidence = prior_decisions(ledger, SPEC.series_id, ("JUMP_EXCEEDED",))

    assert evidence.facts["approved"] == 1
    assert evidence.facts["rejected"] == 0
    assert "빅스텝 인상" in evidence.summary


def test_no_prior_decision_is_reported_plainly(tmp_path):
    ledger = ExceptionLedger(tmp_path / "audit.jsonl")

    evidence = prior_decisions(ledger, SPEC.series_id, ("JUMP_EXCEEDED",))

    assert evidence.facts["approved"] == 0
    assert evidence.verdict is ProbeVerdict.CONTEXT
