"""승인 게이트 — 차단된 건을 사람이 통과시키는 유일한 경로.

품질 위반은 자동으로 서빙을 막는다. 그러나 실제 운영에는 정당한 급변이
있다. 정책금리 인상, 회계기준 변경 같은 것이다. 그래서 차단으로 끝내지
않고 담당자가 근거를 보고 판단할 경로를 둔다.

세 가지를 강제한다.

- 근거 없는 예외는 만들 수 없다. 승인자가 판단할 대상이 없기 때문이다.
- 결정에는 주체와 사유가 반드시 있어야 한다. 나중에 왜 통과시켰는지
  설명할 수 없는 승인은 감사에서 무의미하다.
- 승인에는 만료가 있다. 오래된 승인을 재사용하면 근거가 된 상황이 이미
  지났는데도 계속 통과하게 된다.

Weathervane 프로젝트의 StagedChange와 같은 형태다. 자동 판단은 제안이고
실행은 사람이 승인한다는 원칙을 파이프라인에 이식한 것이다.
"""

import dataclasses
import datetime as dt
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..contracts.quality import QualityFinding


class ExceptionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class StagedException:
    exception_id: str
    series_id: str
    findings: tuple[QualityFinding, ...]
    request_id: str
    created_at: dt.datetime
    expires_at: dt.datetime
    status: ExceptionStatus = ExceptionStatus.PENDING
    decided_by: str = ""
    decision_note: str = ""
    decided_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if not self.findings:
            raise ValueError("findings must not be empty")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")

    def is_expired(self, now: dt.datetime) -> bool:
        return now > self.expires_at


class ExceptionLedger:
    """예외 대장. 모든 상태 변화를 감사 로그에 남긴다."""

    def __init__(self, audit_path: Path) -> None:
        self._audit_path = Path(audit_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, StagedException] = {}

    def _audit(self, action: str, staged: StagedException) -> None:
        entry = {
            "action": action,
            "exception_id": staged.exception_id,
            "series_id": staged.series_id,
            "request_id": staged.request_id,
            "status": str(staged.status),
            "created_at": staged.created_at.isoformat(),
            "expires_at": staged.expires_at.isoformat(),
            "decided_by": staged.decided_by,
            "note": staged.decision_note,
            "decided_at": staged.decided_at.isoformat() if staged.decided_at else None,
            "findings": [
                {
                    "rule": finding.rule,
                    "detail": finding.detail,
                    "period": finding.period.isoformat() if finding.period else None,
                }
                for finding in staged.findings
            ],
        }
        with self._audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def stage(
        self,
        series_id: str,
        findings: list[QualityFinding],
        request_id: str,
        now: dt.datetime,
        ttl: dt.timedelta,
    ) -> StagedException:
        staged = StagedException(
            exception_id=str(uuid.uuid4()),
            series_id=series_id,
            findings=tuple(findings),
            request_id=request_id,
            created_at=now,
            expires_at=now + ttl,
        )
        self._entries[staged.exception_id] = staged
        self._audit("staged", staged)
        return staged

    def get(self, exception_id: str) -> StagedException:
        if exception_id not in self._entries:
            raise KeyError(f"unknown exception: {exception_id}")
        return self._entries[exception_id]

    def list(self, status: ExceptionStatus | None = None) -> list[StagedException]:
        values = list(self._entries.values())
        if status is None:
            return values
        return [entry for entry in values if entry.status is status]

    def _decide(
        self,
        exception_id: str,
        status: ExceptionStatus,
        decided_by: str,
        note: str,
        now: dt.datetime,
    ) -> StagedException:
        staged = self.get(exception_id)
        if staged.status is not ExceptionStatus.PENDING:
            raise ValueError(f"exception {exception_id} is already decided: {staged.status}")
        if not decided_by.strip():
            raise ValueError("decided_by must not be empty")
        if not note.strip():
            raise ValueError("note must not be empty")

        if staged.is_expired(now):
            expired = dataclasses.replace(staged, status=ExceptionStatus.EXPIRED, decided_at=now)
            self._entries[exception_id] = expired
            self._audit("expired", expired)
            raise ValueError(f"exception {exception_id} has expired and cannot be decided")

        decided = dataclasses.replace(
            staged, status=status, decided_by=decided_by, decision_note=note, decided_at=now
        )
        self._entries[exception_id] = decided
        self._audit(str(status), decided)
        return decided

    def approve(
        self, exception_id: str, decided_by: str, note: str, now: dt.datetime
    ) -> StagedException:
        return self._decide(exception_id, ExceptionStatus.APPROVED, decided_by, note, now)

    def reject(
        self, exception_id: str, decided_by: str, note: str, now: dt.datetime
    ) -> StagedException:
        return self._decide(exception_id, ExceptionStatus.REJECTED, decided_by, note, now)

    def is_cleared(self, series_id: str, now: dt.datetime) -> bool:
        """이 시계열에 대해 유효한 승인이 있는지.

        만료된 승인은 통과시키지 않는다.
        """
        return any(
            entry.series_id == series_id
            and entry.status is ExceptionStatus.APPROVED
            and not entry.is_expired(now)
            for entry in self._entries.values()
        )
