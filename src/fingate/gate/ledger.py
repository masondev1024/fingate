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
    # 결정 시점에 승인자가 본 에이전트 권고. 빈 문자열이면 권고 없이 결정했다.
    saw_recommendation: str = ""

    def __post_init__(self) -> None:
        if not self.findings:
            raise ValueError("findings must not be empty")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")

    def is_expired(self, now: dt.datetime) -> bool:
        return now > self.expires_at


class ExceptionLedger:
    """예외 대장. 감사 로그가 진실의 출처다.

    상태를 메모리에만 두면 승인 CLI가 별도 프로세스에서 아무것도 볼 수 없다.
    모든 상태 전이를 append-only 로그에 남기고, 시작할 때 재생해 복원한다.
    감사가 필요한 시스템에서는 로그가 부산물이 아니라 원본이어야 한다.
    """

    def __init__(self, audit_path: Path) -> None:
        self._audit_path = Path(audit_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, StagedException] = {}
        self._replay()

    def _replay(self) -> None:
        """감사 로그를 재생해 대장을 복원한다. 마지막 항목이 최종 상태다."""
        if not self._audit_path.exists():
            return
        for line in self._audit_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            decided_at = entry.get("decided_at")
            self._entries[entry["exception_id"]] = StagedException(
                exception_id=entry["exception_id"],
                series_id=entry["series_id"],
                findings=tuple(
                    QualityFinding(
                        series_id=entry["series_id"],
                        rule=finding["rule"],
                        detail=finding["detail"],
                        period=(
                            dt.date.fromisoformat(finding["period"])
                            if finding.get("period")
                            else None
                        ),
                    )
                    for finding in entry["findings"]
                ),
                request_id=entry["request_id"],
                created_at=dt.datetime.fromisoformat(entry["created_at"]),
                expires_at=dt.datetime.fromisoformat(entry["expires_at"]),
                status=ExceptionStatus(entry["status"]),
                decided_by=entry.get("decided_by", ""),
                decision_note=entry.get("note", ""),
                decided_at=dt.datetime.fromisoformat(decided_at) if decided_at else None,
                saw_recommendation=entry.get("saw_recommendation", ""),
            )

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
            "saw_recommendation": staged.saw_recommendation,
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
        recommendation: str,
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
            staged,
            status=status,
            decided_by=decided_by,
            decision_note=note,
            decided_at=now,
            saw_recommendation=recommendation,
        )
        self._entries[exception_id] = decided
        self._audit(str(status), decided)
        return decided

    def approve(
        self,
        exception_id: str,
        decided_by: str,
        note: str,
        now: dt.datetime,
        recommendation: str = "",
    ) -> StagedException:
        return self._decide(
            exception_id, ExceptionStatus.APPROVED, decided_by, note, now, recommendation
        )

    def reject(
        self,
        exception_id: str,
        decided_by: str,
        note: str,
        now: dt.datetime,
        recommendation: str = "",
    ) -> StagedException:
        return self._decide(
            exception_id, ExceptionStatus.REJECTED, decided_by, note, now, recommendation
        )

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


# 권고값이 함의하는 결정. review 계층의 Recommendation 값과 대응하지만
# 문자열로 둔다. gate가 review에 의존하면 방향이 뒤집힌다. 원장은 에이전트를
# 알지 못한 채로도 동작해야 한다.
_RECOMMENDATION_IMPLIES = {
    "approve_likely": ExceptionStatus.APPROVED,
    "reject_likely": ExceptionStatus.REJECTED,
    "reject_pipeline_defect": ExceptionStatus.REJECTED,
}

# 이보다 적은 표본에서 100% 일치는 거수기가 아니라 그냥 표본 부족이다.
MIN_CONCURRENCE_SAMPLE = 5


@dataclass(frozen=True)
class Concurrence:
    """사람의 결정이 에이전트 권고와 얼마나 일치했는가.

    100% 일치는 좋은 신호가 아니다. 사람이 근거를 보지 않고 권고를 그대로
    누르고 있다는 뜻일 수 있다. 거수기를 탐지할 수 없는 승인 절차는
    승인 절차가 아니다.

    방향이 없는 권고(insufficient_evidence)는 분모에서 뺀다. 일치도 불일치도
    할 수 없는 것을 일치로 세면 비율이 부풀려진다.
    """

    decided_with_recommendation: int
    agreed: int
    disagreed: int

    @property
    def rate(self) -> float | None:
        if not self.decided_with_recommendation:
            return None
        return self.agreed / self.decided_with_recommendation

    @property
    def rubber_stamp_risk(self) -> bool:
        return self.decided_with_recommendation >= MIN_CONCURRENCE_SAMPLE and self.rate == 1.0


def concurrence(ledger: "ExceptionLedger") -> Concurrence:
    agreed = disagreed = 0
    for entry in ledger.list():
        implied = _RECOMMENDATION_IMPLIES.get(entry.saw_recommendation)
        if implied is None or entry.status not in (
            ExceptionStatus.APPROVED,
            ExceptionStatus.REJECTED,
        ):
            continue
        if entry.status is implied:
            agreed += 1
        else:
            disagreed += 1
    return Concurrence(agreed + disagreed, agreed, disagreed)
