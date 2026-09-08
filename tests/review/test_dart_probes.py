import datetime as dt
import json

import pytest

from fingate.collect.raw_store import RawStore
from fingate.contracts.financial_quality import financial_series_id
from fingate.contracts.quality import QualityFinding
from fingate.review.probes import (
    ProbeVerdict,
    blast_radius,
    financial_precedent,
    financial_provenance,
)
from fingate.serve.snapshot import ServingStore
from fingate.warehouse.store import Warehouse

CORP = "00113058"
YEAR = 2024
REPORT = "11014"
NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)


def _rows(assets: int, liabilities: int, equity: int, income: int = 50) -> list[dict]:
    return [
        {"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": str(assets)},
        {"sj_div": "BS", "account_nm": "부채총계", "thstrm_amount": str(liabilities)},
        {"sj_div": "BS", "account_nm": "자본총계", "thstrm_amount": str(equity)},
        {"sj_div": "IS", "account_nm": "당기순이익", "thstrm_amount": str(income)},
    ]


def _put(raw_store: RawStore, rows: list[dict]) -> str:
    body = json.dumps({"status": "000", "message": "정상", "list": rows}, ensure_ascii=False)
    return raw_store.put(
        "dart", "fnlttSinglAcntAll", {"crtfc_key": "s3cret"}, body.encode("utf-8")
    ).request_id


def _finding(rule: str) -> QualityFinding:
    return QualityFinding(financial_series_id(CORP), rule, "detail", dt.date(YEAR, 9, 30))


@pytest.fixture
def raw_store(tmp_path):
    return RawStore(tmp_path)


@pytest.fixture
def warehouse():
    with Warehouse() as store:
        yield store


# --- financial_provenance ---------------------------------------------------


def test_a_violation_the_source_really_sent_is_context(raw_store):
    """원본을 다시 해석해도 같은 위반이 나오면 출처가 그렇게 보낸 것이다."""
    request_id = _put(raw_store, _rows(assets=1_000, liabilities=800, equity=100))

    evidence = financial_provenance(
        raw_store, CORP, YEAR, REPORT, [_finding("BALANCE_SHEET_BROKEN")], request_id
    )

    assert evidence.verdict is ProbeVerdict.CONTEXT
    assert "BALANCE_SHEET_BROKEN" in evidence.facts["raw_rules"]


def test_a_violation_absent_from_the_source_is_our_defect(raw_store):
    """원본은 정합한데 우리 쪽만 깨졌으면 승인 대상이 아니라 버그다."""
    request_id = _put(raw_store, _rows(assets=1_000, liabilities=800, equity=200))

    evidence = financial_provenance(
        raw_store, CORP, YEAR, REPORT, [_finding("BALANCE_SHEET_BROKEN")], request_id
    )

    assert evidence.verdict is ProbeVerdict.SUPPORTS_DEFECT
    assert evidence.facts["raw_rules"] == []


def test_a_missing_raw_record_is_insufficient(raw_store):
    evidence = financial_provenance(
        raw_store, CORP, YEAR, REPORT, [_finding("BALANCE_SHEET_BROKEN")], "gone"
    )

    assert evidence.verdict is ProbeVerdict.INSUFFICIENT


def test_a_body_that_is_not_a_dart_payload_is_insufficient(raw_store):
    request_id = raw_store.put("dart", "x", {}, b"<html>not json</html>").request_id

    evidence = financial_provenance(
        raw_store, CORP, YEAR, REPORT, [_finding("BALANCE_SHEET_BROKEN")], request_id
    )

    assert evidence.verdict is ProbeVerdict.INSUFFICIENT


def test_a_different_violation_in_the_source_is_still_reported(raw_store):
    """원본에서 다른 위반이 나오면 그 사실을 숨기지 않는다."""
    request_id = _put(raw_store, _rows(assets=-1_000, liabilities=-800, equity=-200))

    evidence = financial_provenance(
        raw_store, CORP, YEAR, REPORT, [_finding("BALANCE_SHEET_BROKEN")], request_id
    )

    assert "NEGATIVE_STOCK" in evidence.facts["raw_rules"]


def test_financial_provenance_never_leaks_credentials(raw_store):
    request_id = _put(raw_store, _rows(assets=1_000, liabilities=800, equity=200))

    evidence = financial_provenance(
        raw_store, CORP, YEAR, REPORT, [_finding("BALANCE_SHEET_BROKEN")], request_id
    )

    assert "s3cret" not in json.dumps(evidence.facts, ensure_ascii=False) + evidence.summary


# --- financial_precedent ----------------------------------------------------


def test_precedent_reports_the_corps_loaded_history(warehouse):
    warehouse.load_financials(
        corp_code=CORP,
        corp_name="한화생명",
        bsns_year=2023,
        report_code="11011",
        rows=_rows(assets=1_000, liabilities=800, equity=200),
        request_id="r",
        loaded_at=NOW,
    )

    evidence = financial_precedent(warehouse, CORP)

    assert evidence.verdict is ProbeVerdict.CONTEXT
    assert evidence.facts["loaded_quarters"] == 1


def test_precedent_on_a_corp_with_no_history_says_so(warehouse):
    evidence = financial_precedent(warehouse, CORP)

    assert evidence.facts["loaded_quarters"] == 0
    assert evidence.facts["max_asset_change"] is None


# --- blast_radius must understand DART ------------------------------------


def test_blast_radius_counts_serving_rows_for_the_blocked_insurer(warehouse, tmp_path):
    """재무가 서빙 테이블의 구동 테이블이므로 차단의 비용이 금리와 다르다."""
    for year in (2023, 2024):
        warehouse.load_financials(
            corp_code=CORP,
            corp_name="한화생명",
            bsns_year=year,
            report_code="11011",
            rows=_rows(assets=1_000, liabilities=800, equity=200),
            request_id="r",
            loaded_at=NOW,
        )

    evidence = blast_radius(warehouse, ServingStore(tmp_path), financial_series_id(CORP), NOW)

    assert evidence.facts["serving_rows_affected"] == 2


# --- assemble 은 DART 예외를 올바른 probe 로 보내야 한다 -----------------------


def test_reviewing_a_dart_exception_uses_the_financial_probes(raw_store, warehouse, tmp_path):
    from fingate.gate.ledger import ExceptionLedger
    from fingate.review.assemble import review_exception
    from fingate.review.recommend import Recommendation

    request_id = _put(raw_store, _rows(assets=1_000, liabilities=800, equity=200))
    ledger = ExceptionLedger(tmp_path / "audit.jsonl")
    staged = ledger.stage(
        financial_series_id(CORP),
        [_finding("BALANCE_SHEET_BROKEN")],
        request_id,
        NOW,
        dt.timedelta(days=7),
    )

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=ServingStore(tmp_path / "serving"),
        now=NOW,
    )

    probes = {item.probe for item in review.evidence}
    assert "financial_provenance" in probes
    assert "financial_precedent" in probes
    assert "raw_provenance" not in probes, "금리용 probe 가 재무에 쓰이면 항상 대조 실패한다"
    # 원본은 정합한데 위반이 기록돼 있으므로 우리 쪽 결함이다.
    assert review.recommendation.verdict is Recommendation.REJECT


def test_a_dart_exception_reports_that_peers_do_not_apply(raw_store, warehouse, tmp_path):
    from fingate.gate.ledger import ExceptionLedger
    from fingate.review.assemble import review_exception
    from fingate.review.probes import ProbeVerdict

    request_id = _put(raw_store, _rows(assets=1_000, liabilities=800, equity=100))
    ledger = ExceptionLedger(tmp_path / "audit.jsonl")
    staged = ledger.stage(
        financial_series_id(CORP),
        [_finding("BALANCE_SHEET_BROKEN")],
        request_id,
        NOW,
        dt.timedelta(days=7),
    )

    review = review_exception(
        staged.exception_id,
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=ServingStore(tmp_path / "serving"),
        now=NOW,
    )
    peer = next(item for item in review.evidence if item.probe == "peer_corroboration")

    assert peer.verdict is ProbeVerdict.INSUFFICIENT
