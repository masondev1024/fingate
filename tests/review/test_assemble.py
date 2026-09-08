import datetime as dt
import json

import pytest

from fingate.collect.raw_store import RawStore
from fingate.contracts.quality import QualityFinding
from fingate.contracts.schema import Observation
from fingate.gate.ledger import ExceptionLedger
from fingate.review.assemble import review_exception
from fingate.review.recommend import Recommendation
from fingate.serve.snapshot import ServingStore
from fingate.warehouse.store import Warehouse

NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
SERIES = "base_rate_daily"
PEER = "call_rate_daily"
START = dt.date(2026, 1, 1)


@pytest.fixture
def bench(tmp_path):
    """의심 관측 하나가 차단된 상태를 실제 구성 요소로 만든다."""
    warehouse = Warehouse()
    raw_store = RawStore(tmp_path / "raw")
    serving = ServingStore(tmp_path / "serving")
    ledger = ExceptionLedger(tmp_path / "audit.jsonl")
    yield warehouse, raw_store, serving, ledger
    warehouse.close()


def _series(jump: float, peer_jump: float) -> tuple[list[float], list[float]]:
    subject, peer, level = [], [], 3.0
    for day in range(120):
        if day and day % 20 == 0:
            level += 0.25
        subject.append(level)
        peer.append(level + 0.5)
    subject.append(subject[-1] + jump)
    peer.append(peer[-1] + peer_jump)
    return subject, peer


def _load(warehouse, series_id, values, request_id="req-1"):
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
        NOW,
    )


def _stage(ledger, suspect: dt.date, request_id: str):
    finding = QualityFinding(SERIES, "JUMP_EXCEEDED", "0.85 exceeds 0.75", suspect)
    return ledger.stage(SERIES, [finding], request_id, NOW, dt.timedelta(days=7))


def _put_raw(raw_store, suspect: dt.date, value: float) -> str:
    body = json.dumps(
        {
            "StatisticSearch": {
                "list_total_count": 1,
                "row": [{"TIME": suspect.strftime("%Y%m%d"), "DATA_VALUE": f"{value}"}],
            }
        }
    )
    return raw_store.put("ecos", "StatisticSearch", {}, body.encode("utf-8")).request_id


def test_a_corroborated_jump_is_recommended_for_approval(bench):
    warehouse, raw_store, serving, ledger = bench
    subject, peer = _series(jump=0.85, peer_jump=0.80)
    suspect = START + dt.timedelta(days=len(subject) - 1)
    request_id = _put_raw(raw_store, suspect, subject[-1])
    _load(warehouse, SERIES, subject, request_id)
    _load(warehouse, PEER, peer, request_id)
    staged = _stage(ledger, suspect, request_id)

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=serving,
        now=NOW,
    )

    assert review.recommendation.verdict is Recommendation.APPROVE_LIKELY
    assert {item.probe for item in review.evidence} == {
        "peer_corroboration",
        "anchor_spread",
        "raw_provenance",
        "historical_precedent",
        "blast_radius",
        "prior_decisions",
    }


def test_a_lone_spike_is_recommended_for_rejection(bench):
    warehouse, raw_store, serving, ledger = bench
    subject, peer = _series(jump=0.85, peer_jump=0.0)
    suspect = START + dt.timedelta(days=len(subject) - 1)
    request_id = _put_raw(raw_store, suspect, subject[-1])
    _load(warehouse, SERIES, subject, request_id)
    _load(warehouse, PEER, peer, request_id)
    staged = _stage(ledger, suspect, request_id)

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=serving,
        now=NOW,
    )

    assert review.recommendation.verdict is Recommendation.REJECT_LIKELY


def test_a_value_absent_from_raw_overrides_peer_support(bench):
    """peer가 동행해도 원본에 없는 값은 반려로 뒤집힌다."""
    warehouse, raw_store, serving, ledger = bench
    subject, peer = _series(jump=0.85, peer_jump=0.80)
    suspect = START + dt.timedelta(days=len(subject) - 1)
    request_id = _put_raw(raw_store, suspect, subject[-1] - 0.5)  # 원본은 다른 값
    _load(warehouse, SERIES, subject, request_id)
    _load(warehouse, PEER, peer, request_id)
    staged = _stage(ledger, suspect, request_id)

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=serving,
        now=NOW,
    )

    assert review.recommendation.verdict is Recommendation.REJECT


def test_the_reviewer_cannot_change_the_ledger(bench):
    """reviewer가 승인·반려를 실행할 수 있으면 사람의 승인이 형식이 된다.

    코드 규약이 아니라 실행으로 막는다.
    """
    warehouse, raw_store, serving, ledger = bench
    subject, peer = _series(jump=0.85, peer_jump=0.80)
    suspect = START + dt.timedelta(days=len(subject) - 1)
    request_id = _put_raw(raw_store, suspect, subject[-1])
    _load(warehouse, SERIES, subject, request_id)
    _load(warehouse, PEER, peer, request_id)
    staged = _stage(ledger, suspect, request_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("reviewer가 원장을 변경하려 했다")

    ledger.approve = forbidden
    ledger.reject = forbidden
    ledger.stage = forbidden

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=serving,
        now=NOW,
    )

    assert review.recommendation.verdict is Recommendation.APPROVE_LIKELY
    assert ledger.get(staged.exception_id).status == "pending"


def test_an_already_decided_exception_can_still_be_reviewed(bench):
    """사후 감사가 목적이다. 결정이 끝난 건도 근거를 다시 볼 수 있어야 한다."""
    warehouse, raw_store, serving, ledger = bench
    subject, peer = _series(jump=0.85, peer_jump=0.80)
    suspect = START + dt.timedelta(days=len(subject) - 1)
    request_id = _put_raw(raw_store, suspect, subject[-1])
    _load(warehouse, SERIES, subject, request_id)
    _load(warehouse, PEER, peer, request_id)
    staged = _stage(ledger, suspect, request_id)
    ledger.approve(staged.exception_id, decided_by="mason", note="확인함", now=NOW)

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=serving,
        now=NOW,
    )

    assert review.exception.status == "approved"
    assert review.recommendation.verdict is Recommendation.APPROVE_LIKELY


def test_a_finding_without_a_period_yields_no_recommendation(bench):
    """NO_OBSERVATIONS처럼 시점이 없는 위반은 대조할 관측 자체가 없다."""
    warehouse, raw_store, serving, ledger = bench
    finding = QualityFinding(SERIES, "NO_OBSERVATIONS", "series produced nothing", None)
    staged = ledger.stage(SERIES, [finding], "req", NOW, dt.timedelta(days=7))

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=serving,
        now=NOW,
    )

    assert review.recommendation.verdict is Recommendation.INSUFFICIENT_EVIDENCE
