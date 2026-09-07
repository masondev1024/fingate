"""응답 계약 — HTTP 200은 성공을 의미하지 않는다.

2026-09-07 실측: 한국은행 ECOS와 금감원 DART는 인증 실패, 존재하지 않는
통계표 코드 등 모든 오류에 HTTP 200을 반환한다. DART corpCode.xml은
정상 3,601,634바이트 zip 자리에 오류 시 150바이트 XML을 같은 200으로 준다.

따라서 성공 판정은 상태 코드가 아니라 본문으로 한다. 이 계층을 통과하지
못한 응답은 데이터로 취급하지 않는다.
"""

import json
from dataclasses import dataclass

ZIP_MAGIC = b"PK\x03\x04"


@dataclass(frozen=True)
class ResponseVerdict:
    """응답이 데이터로 취급될 자격이 있는지에 대한 판정."""

    ok: bool
    source: str
    code: str | None
    message: str | None

    @classmethod
    def failure(cls, source: str, code: str, message: str) -> "ResponseVerdict":
        return cls(ok=False, source=source, code=code, message=message)

    @classmethod
    def success(cls, source: str) -> "ResponseVerdict":
        return cls(ok=True, source=source, code=None, message=None)


def _load_json(body: bytes, source: str) -> tuple[dict | None, ResponseVerdict | None]:
    if not body:
        return None, ResponseVerdict.failure(source, "EMPTY_BODY", "response body is empty")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, ResponseVerdict.failure(source, "NOT_JSON", f"body is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        return None, ResponseVerdict.failure(source, "NOT_OBJECT", "top level is not an object")
    return parsed, None


def verify_ecos(body: bytes) -> ResponseVerdict:
    """ECOS 응답 판정.

    오류일 때만 최상위에 RESULT 키가 실린다. 정상 응답에는 서비스명이 온다.
    """
    parsed, failure = _load_json(body, "ecos")
    if failure is not None:
        return failure
    result = parsed.get("RESULT")
    if isinstance(result, dict):
        return ResponseVerdict.failure(
            "ecos", str(result.get("CODE", "UNKNOWN")), str(result.get("MESSAGE", ""))
        )
    if not parsed:
        return ResponseVerdict.failure("ecos", "EMPTY_OBJECT", "no service payload present")
    return ResponseVerdict.success("ecos")


def verify_dart_json(body: bytes) -> ResponseVerdict:
    """DART JSON 응답 판정. status가 "000"일 때만 정상이다.

    status 필드가 없으면 성공으로 간주하지 않는다. 계약이 바뀐 것일 수도,
    다른 엔드포인트의 응답이 섞인 것일 수도 있다. 둘 다 적재하면 안 된다.
    """
    parsed, failure = _load_json(body, "dart")
    if failure is not None:
        return failure
    if "status" not in parsed:
        return ResponseVerdict.failure("dart", "NO_STATUS", "response has no status field")
    status = str(parsed["status"])
    if status != "000":
        return ResponseVerdict.failure("dart", status, str(parsed.get("message", "")))
    return ResponseVerdict.success("dart")


def verify_dart_zip(body: bytes, min_bytes: int) -> ResponseVerdict:
    """DART zip 응답 판정.

    시그니처와 최소 크기를 함께 본다. 시그니처만 보면 잘린 응답을 통과시키고,
    크기만 보면 오류 XML이 충분히 길 때 통과시킨다.
    """
    if not body.startswith(ZIP_MAGIC):
        head = body[:80].decode("utf-8", errors="replace")
        return ResponseVerdict.failure("dart", "NOT_ZIP", f"body is not a zip archive: {head!r}")
    if len(body) < min_bytes:
        return ResponseVerdict.failure(
            "dart", "ZIP_TOO_SMALL", f"zip is {len(body)} bytes, expected at least {min_bytes}"
        )
    return ResponseVerdict.success("dart")
