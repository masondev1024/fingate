"""응답 계약 테스트.

픽스처는 2026-09-07에 실제 API를 호출해 관측한 본문이다. 지어낸 것이 아니다.
두 API 모두 인증 실패·잘못된 코드에 HTTP 200을 반환하므로, 상태 코드로는
성공을 판정할 수 없다.
"""

import dataclasses

import pytest

from fingate.contracts.response import (
    ResponseVerdict,
    verify_dart_json,
    verify_dart_zip,
    verify_ecos,
)

# --- 실제 관측 본문 ---
ECOS_AUTH_ERROR = (
    b'{"RESULT":{"CODE":"INFO-100","MESSAGE":'
    b'"\xec\x9d\xb8\xec\xa6\x9d\xed\x82\xa4\xea\xb0\x80 '
    b"\xec\x9c\xa0\xed\x9a\xa8\xed\x95\x98\xec\xa7\x80 "
    b'\xec\x95\x8a\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4."}}'
)
ECOS_SUCCESS = (
    b'{"StatisticTableList":{"list_total_count":839,'
    b'"row":[{"STAT_CODE":"0000000001","STAT_NAME":"1."}]}}'
)
DART_AUTH_ERROR = b'{"status":"010","message":"unregistered key"}'
DART_SUCCESS = b'{"status":"000","message":"\xec\xa0\x95\xec\x83\x81","list":[{"account_nm":"x"}]}'
DART_ZIP_ERROR_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b"<result><status>010</status><message>unregistered key</message></result>"
)
ZIP_MAGIC = b"PK\x03\x04"


def test_ecos_error_body_is_rejected_despite_http_200():
    verdict = verify_ecos(ECOS_AUTH_ERROR)
    assert verdict.ok is False
    assert verdict.code == "INFO-100"
    assert "인증키" in verdict.message


def test_ecos_success_body_is_accepted():
    verdict = verify_ecos(ECOS_SUCCESS)
    assert verdict.ok is True
    assert verdict.code is None


def test_ecos_rejects_non_json():
    verdict = verify_ecos(b"<html>maintenance</html>")
    assert verdict.ok is False
    assert verdict.code == "NOT_JSON"


def test_ecos_rejects_empty_body():
    verdict = verify_ecos(b"")
    assert verdict.ok is False


def test_dart_error_status_is_rejected_despite_http_200():
    verdict = verify_dart_json(DART_AUTH_ERROR)
    assert verdict.ok is False
    assert verdict.code == "010"


def test_dart_success_status_is_accepted():
    assert verify_dart_json(DART_SUCCESS).ok is True


def test_dart_rejects_missing_status_field():
    """status 필드가 없으면 성공으로 간주하지 않는다."""
    verdict = verify_dart_json(b'{"list":[]}')
    assert verdict.ok is False
    assert verdict.code == "NO_STATUS"


def test_dart_zip_rejects_error_xml_served_as_zip():
    """3.6MB zip 자리에 150바이트 오류 XML이 HTTP 200으로 온다 (실측)."""
    verdict = verify_dart_zip(DART_ZIP_ERROR_XML, min_bytes=1024)
    assert verdict.ok is False
    assert verdict.code == "NOT_ZIP"


def test_dart_zip_rejects_undersized_zip():
    """시그니처는 맞지만 크기가 비정상이면 거부한다."""
    verdict = verify_dart_zip(ZIP_MAGIC + b"tiny", min_bytes=1024)
    assert verdict.ok is False
    assert verdict.code == "ZIP_TOO_SMALL"


def test_dart_zip_accepts_plausible_zip():
    verdict = verify_dart_zip(ZIP_MAGIC + b"x" * 2000, min_bytes=1024)
    assert verdict.ok is True


def test_verdict_is_immutable():
    verdict = ResponseVerdict(ok=True, source="ecos", code=None, message=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.ok = False
