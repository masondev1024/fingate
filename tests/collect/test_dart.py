"""DART 수집기 테스트.

corpCode.xml은 정상 3.6MB zip, 오류 시 150바이트 XML을 같은 HTTP 200으로
반환한다(2026-09-07 실측). 크기와 시그니처를 함께 검사하지 않으면
빈 데이터가 적재된다.
"""

import io
import json
import zipfile

import pytest

from fingate.collect.dart import DartCollector, ReportCode
from fingate.collect.ecos import ContractViolation
from fingate.collect.raw_store import RawStore

API_KEY = "SECRET_DART_KEY"

# 실측한 오류 본문 (150 bytes)
ERROR_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b"<result><status>010</status><message>unregistered key</message></result>"
)

CORP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list><corp_code>00113058</corp_code><corp_name>한화생명</corp_name>
        <stock_code>088350</stock_code><modify_date>20240101</modify_date></list>
  <list><corp_code>00135917</corp_code><corp_name>한화손해보험</corp_name>
        <stock_code>000370</stock_code><modify_date>20240101</modify_date></list>
  <list><corp_code>00999999</corp_code><corp_name>비상장회사</corp_name>
        <stock_code></stock_code><modify_date>20240101</modify_date></list>
</result>""".encode()


def _zip_bytes(payload: bytes, pad_to: int = 4096) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", payload + b"\n" + b" " * pad_to)
    return buffer.getvalue()


FINANCIAL_BODY = json.dumps(
    {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "bsns_year": "2023",
                "corp_code": "00113058",
                "sj_div": "BS",
                "sj_nm": "재무상태표",
                "account_nm": "자산총계",
                "thstrm_amount": "160000000000000",
            }
        ],
    }
).encode("utf-8")


def _collector(tmp_path, body: bytes):
    captured: list[str] = []

    def fetch(url: str) -> bytes:
        captured.append(url)
        return body

    return DartCollector(API_KEY, RawStore(tmp_path), fetch=fetch), captured


# --- corpCode ---


def test_parses_corp_codes_from_zip(tmp_path):
    collector, _ = _collector(tmp_path, _zip_bytes(CORP_XML))
    result = collector.collect_corp_codes(min_zip_bytes=100)
    assert len(result.entries) == 3
    assert result.entries[0].corp_code == "00113058"
    assert result.entries[0].corp_name == "한화생명"


def test_listed_only_filter_drops_unlisted(tmp_path):
    collector, _ = _collector(tmp_path, _zip_bytes(CORP_XML))
    result = collector.collect_corp_codes(listed_only=True, min_zip_bytes=100)
    assert [e.corp_name for e in result.entries] == ["한화생명", "한화손해보험"]


def test_error_xml_served_as_zip_is_rejected(tmp_path):
    """이것이 이 계층의 존재 이유다. HTTP 200, 150바이트, zip이 아님."""
    collector, _ = _collector(tmp_path, ERROR_XML)
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect_corp_codes()
    assert excinfo.value.code == "NOT_ZIP"


def test_undersized_zip_is_rejected(tmp_path):
    collector, _ = _collector(tmp_path, _zip_bytes(b"<result/>", pad_to=0))
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect_corp_codes(min_zip_bytes=100_000)
    assert excinfo.value.code == "ZIP_TOO_SMALL"


def test_rejected_zip_is_still_preserved(tmp_path):
    collector, _ = _collector(tmp_path, ERROR_XML)
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect_corp_codes()
    meta = json.loads(excinfo.value.raw.meta_path.read_text(encoding="utf-8"))
    assert meta["ok"] is False
    assert meta["byte_size"] == len(ERROR_XML)


# --- 재무제표 ---


def test_returns_financial_rows(tmp_path):
    collector, _ = _collector(tmp_path, FINANCIAL_BODY)
    result = collector.collect_financials("00113058", 2023, ReportCode.ANNUAL)
    assert len(result.rows) == 1
    assert result.rows[0]["account_nm"] == "자산총계"


def test_builds_financial_url_with_report_code(tmp_path):
    collector, captured = _collector(tmp_path, FINANCIAL_BODY)
    collector.collect_financials("00113058", 2023, ReportCode.ANNUAL)
    assert "corp_code=00113058" in captured[0]
    assert "bsns_year=2023" in captured[0]
    assert "reprt_code=11011" in captured[0]


def test_error_status_is_rejected(tmp_path):
    collector, _ = _collector(tmp_path, b'{"status":"010","message":"unregistered key"}')
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect_financials("00113058", 2023, ReportCode.ANNUAL)
    assert excinfo.value.code == "010"


def test_no_data_status_is_a_violation_not_empty_success(tmp_path):
    """조회 결과 없음(013)을 빈 성공으로 처리하면 결측이 조용히 퍼진다."""
    collector, _ = _collector(tmp_path, b'{"status":"013","message":"no data"}')
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect_financials("00113058", 2019, ReportCode.ANNUAL)
    assert excinfo.value.code == "013"


def test_missing_list_is_a_violation(tmp_path):
    collector, _ = _collector(tmp_path, b'{"status":"000","message":"ok"}')
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect_financials("00113058", 2023, ReportCode.ANNUAL)
    assert excinfo.value.code == "NO_ROWS"


def test_api_key_never_leaks(tmp_path):
    collector, _ = _collector(tmp_path, b'{"status":"010","message":"bad"}')
    with pytest.raises(ContractViolation) as excinfo:
        collector.collect_financials("00113058", 2023, ReportCode.ANNUAL)
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in excinfo.value.raw.meta_path.read_text(encoding="utf-8")
    assert API_KEY not in str(excinfo.value.raw.path)
