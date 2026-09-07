"""원본 응답 보존과 계보.

두 가지 불변식을 지킨다.

1. 자격 증명은 저장된 어디에도 남지 않는다. 경로, 메타데이터, 오류 메시지 모두.
   ASK-Seoul에서 KMA serviceKey를 raw path와 로그에 남기지 않도록 한 것과 같은 규율이다.
2. 원본은 계약 위반이어도 지우지 않는다. 무엇이 왜 거부됐는지 추적할 수 없으면
   게이트는 블랙박스가 되고, 재현 가능한 백필도 불가능해진다.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REDACTED = "***REDACTED***"

# 이름에 이런 조각이 들어간 파라미터는 값을 저장하지 않는다.
# 화이트리스트가 아니라 블랙리스트인 이유는, 새 자격 증명 파라미터가 생겼을 때
# 실수로 통과시키는 쪽보다 과하게 가리는 쪽이 안전하기 때문이다.
_CREDENTIAL_HINTS = ("key", "token", "secret", "password", "passwd", "credential", "auth")


@dataclass(frozen=True)
class RawRecord:
    request_id: str
    source: str
    endpoint: str
    sha256: str
    byte_size: int
    received_at: str
    path: Path
    meta_path: Path


def redact_params(params: dict[str, object]) -> dict[str, object]:
    """자격 증명으로 보이는 파라미터 값을 가린다."""
    redacted: dict[str, object] = {}
    for name, value in params.items():
        lowered = name.lower()
        redacted[name] = REDACTED if any(h in lowered for h in _CREDENTIAL_HINTS) else value
    return redacted


def _safe_segment(text: str) -> str:
    """경로에 쓸 수 있는 형태로 정리한다. 경로 조작과 예기치 않은 문자를 막는다."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", text)
    return cleaned[:64] or "unknown"


class RawStore:
    """수신한 원본을 파일로 보존하고 계보 메타데이터를 함께 남긴다."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _dir_for(self, source: str, received_at: datetime) -> Path:
        target = self._root / _safe_segment(source) / received_at.strftime("%Y-%m-%d")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def put(
        self,
        source: str,
        endpoint: str,
        params: dict[str, object],
        body: bytes,
        *,
        ok: bool = True,
        verdict_code: str | None = None,
    ) -> RawRecord:
        received_at = datetime.now(UTC)
        request_id = str(uuid.uuid4())
        directory = self._dir_for(source, received_at)

        stem = f"{_safe_segment(endpoint)}__{request_id}"
        path = directory / f"{stem}.body"
        meta_path = directory / f"{stem}.meta.json"

        path.write_bytes(body)
        record = RawRecord(
            request_id=request_id,
            source=source,
            endpoint=endpoint,
            sha256=hashlib.sha256(body).hexdigest(),
            byte_size=len(body),
            received_at=received_at.isoformat(),
            path=path,
            meta_path=meta_path,
        )
        meta = {
            "request_id": record.request_id,
            "source": record.source,
            "endpoint": record.endpoint,
            "params": redact_params(params),
            "sha256": record.sha256,
            "byte_size": record.byte_size,
            "received_at": record.received_at,
            "ok": ok,
            "verdict_code": verdict_code,
            "body_file": path.name,
        }
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return record

    def get_body(self, request_id: str) -> bytes:
        for meta_path in self._root.rglob("*.meta.json"):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("request_id") == request_id:
                return (meta_path.parent / meta["body_file"]).read_bytes()
        raise KeyError(f"unknown request_id: {request_id}")
