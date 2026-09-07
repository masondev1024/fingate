"""ECOS 수집기 테스트.

전송 계층을 주입 가능하게 두어 네트워크 없이 계약 동작을 검증한다.
실제 API 호출은 별도 스모크로 확인한다.
"""

import json

import pytest

from fingate.collect.ecos import ContractViolation, EcosCollector, EcosRequest
from fingate.collect.raw_store import RawStore

API_KEY = "SECRET_ECOS_KEY"

SUCCESS_BODY = json.dumps(
    {
        "StatisticSearch": {
            "list_total_count": 2,
            "row": [
                {"STAT_CODE": "722Y001", "TIME": "20240102", "DATA_VALUE": "3.5", "UNIT_NAME": "%"},
                {"STAT_CODE": "722Y001", "TIME": "20240103", "DATA_VALUE": "3.5", "UNIT_NAME": "%"},
            ],
        }
    }
).encode("utf-8")

ERROR_BODY = json.dumps(
    {"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다."}}
).encode("utf-8")


def _request() -> EcosRequest:
    return EcosRequest(
        stat_code="722Y001",
        cycle="D",
        start_period="20240101",
        end_period="20240103",
        item_code="0101000",
    )


def _collector(tmp_path, body: bytes):
    captured: list[str] = []

    def fetch(url: str) -> bytes:
        captured.append(url)
        return body

    collector = EcosCollector(api_key=API_KEY, raw_store=RawStore(tmp_path), fetch=fetch)
    return collector, captured


def test_builds_url_in_ecos_path_order(tmp_path):
    collector, captured = _collector(tmp_path, SUCCESS_BODY)
    collector.collect(_request())
    url = captured[0]
    assert url.startswith("https://ecos.bok.or.kr/api/StatisticSearch/")
    assert url.endswith("/json/kr/1/1000/722Y001/D/20240101/20240103/0101000")


def test_omits_item_code_when_absent(tmp_path):
    collector, captured = _collector(tmp_path, SUCCESS_BODY)
    collector.collect(
        EcosRequest(stat_code="722Y001", cycle="D", start_period="20240101", end_period="20240103")
    )
    assert captured[0].endswith("/722Y001/D/20240101/20240103")


def test_returns_rows_on_success(tmp_path):
    collector, _ = _collector(tmp_path, SUCCESS_BODY)
    result = collector.collect(_request())
    assert len(result.rows) == 2
    assert result.rows[0]["DATA_VALUE"] == "3.5"
    assert result.total_count == 2


def test_success_is_preserved_with_lineage(tmp_path):
    collector, _ = _collector(tmp_path, SUCCESS_BODY)
    result = collector.collect(_request())
    meta = json.loads(result.raw.meta_path.read_text(encoding="utf-8"))
    assert meta["ok"] is True
    assert meta["source"] == "ecos"
    assert meta["request_id"] == result.raw.request_id


def test_error_payload_raises_instead_of_returning_data(tmp_path):
    """HTTP 200이어도 오류 본문이면 데이터를 돌려주지 않는다."""
    collector, _ = _collector(tmp_path, ERROR_BODY)
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect(_request())
    assert excinfo.value.code == "INFO-100"


def test_error_payload_is_still_preserved(tmp_path):
    collector, _ = _collector(tmp_path, ERROR_BODY)
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect(_request())
    meta = json.loads(excinfo.value.raw.meta_path.read_text(encoding="utf-8"))
    assert meta["ok"] is False
    assert meta["verdict_code"] == "INFO-100"


def test_non_json_body_is_rejected(tmp_path):
    collector, _ = _collector(tmp_path, b"<html>maintenance</html>")
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect(_request())
    assert excinfo.value.code == "NOT_JSON"


def test_api_key_never_appears_in_exception(tmp_path):
    collector, _ = _collector(tmp_path, ERROR_BODY)
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect(_request())
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(excinfo.value)


def test_api_key_never_appears_in_stored_metadata(tmp_path):
    collector, _ = _collector(tmp_path, SUCCESS_BODY)
    result = collector.collect(_request())
    assert API_KEY not in result.raw.meta_path.read_text(encoding="utf-8")
    assert API_KEY not in str(result.raw.path)


def test_missing_row_key_is_a_contract_violation(tmp_path):
    """성공 판정을 받았어도 기대한 구조가 없으면 데이터로 쓰지 않는다."""
    body = json.dumps({"StatisticSearch": {"list_total_count": 0}}).encode("utf-8")
    collector, _ = _collector(tmp_path, body)
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect(_request())
    assert excinfo.value.code == "NO_ROWS"
