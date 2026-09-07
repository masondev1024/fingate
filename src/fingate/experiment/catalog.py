"""오염 카탈로그.

설계 문서(커밋 88a07c9, 2026-09-07 10:48)에 8종을 사전 등록했고 게이트는
그 이후(10:55~11:54)에 구현했다. 결과를 보고 카탈로그를 조정하지 않았다.

101(DART 접근 제한)은 개발 중 발견한 사후 추가다. 사전 등록분과 구분한다.
"""

from dataclasses import dataclass
from enum import StrEnum


class Origin(StrEnum):
    OBSERVED = "observed"  # 실제 API가 반환하는 것을 관측
    INJECTED = "injected"  # 정상 데이터에 인위적으로 주입


class Layer(StrEnum):
    RESPONSE = "response"
    SCHEMA = "schema"
    QUALITY = "quality"


class SourceKind(StrEnum):
    """어떤 응답 계약으로 검증해야 하는가. 엔드포인트마다 다르다."""

    ECOS_JSON = "ecos_json"
    DART_JSON = "dart_json"
    DART_ZIP = "dart_zip"


@dataclass(frozen=True)
class Corruption:
    corruption_id: str
    name: str
    origin: Origin
    expected_layer: Layer
    pre_registered: bool
    evidence: str
    source_kind: SourceKind = SourceKind.ECOS_JSON


CATALOG: tuple[Corruption, ...] = (
    Corruption(
        "ecos_auth_error",
        "ECOS 인증 실패 응답",
        Origin.OBSERVED,
        Layer.RESPONSE,
        True,
        "HTTP 200 + RESULT.CODE=INFO-100",
    ),
    Corruption(
        "ecos_unknown_table",
        "존재하지 않는 통계표 코드",
        Origin.OBSERVED,
        Layer.RESPONSE,
        True,
        "HTTP 200 + RESULT.CODE=INFO-200",
    ),
    Corruption(
        "dart_zip_error_xml",
        "3.6MB zip 자리에 150B 오류 XML",
        Origin.OBSERVED,
        Layer.RESPONSE,
        True,
        "HTTP 200, 정상 3,601,634B 대비 150B",
        SourceKind.DART_ZIP,
    ),
    Corruption(
        "value_outlier",
        "값 이상치",
        Origin.INJECTED,
        Layer.QUALITY,
        True,
        "기준금리 999%",
    ),
    Corruption(
        "stale_series",
        "신선도 위반",
        Origin.INJECTED,
        Layer.QUALITY,
        True,
        "SLA 초과 경과",
    ),
    Corruption(
        "duplicate_period",
        "중복 키",
        Origin.INJECTED,
        Layer.QUALITY,
        True,
        "같은 기간 2회",
    ),
    Corruption(
        "schema_field_removed",
        "스키마 변경 — 필드 삭제",
        Origin.INJECTED,
        Layer.SCHEMA,
        True,
        "TIME 필드 제거",
    ),
    Corruption(
        "schema_type_changed",
        "스키마 변경 — 타입 변경",
        Origin.INJECTED,
        Layer.SCHEMA,
        True,
        "DATA_VALUE 를 비수치로",
    ),
    Corruption(
        "partial_missing",
        "부분 결측",
        Origin.INJECTED,
        Layer.SCHEMA,
        True,
        "DATA_VALUE 빈 문자열",
    ),
    Corruption(
        "dart_throttled",
        "DART 접근 제한",
        Origin.OBSERVED,
        Layer.RESPONSE,
        False,  # 개발 중 발견한 사후 추가
        "HTTP 200 + status=101, 무페이싱 25건 연속 호출 후",
        SourceKind.DART_JSON,
    ),
    Corruption(
        "series_swapped",
        "다른 시계열이 반환됨",
        Origin.INJECTED,
        Layer.SCHEMA,
        False,  # 스키마 계약 설계 중 추가
        "ITEM_CODE1 불일치, 값은 정상으로 보임",
    ),
)

PRE_REGISTERED = tuple(c for c in CATALOG if c.pre_registered)
