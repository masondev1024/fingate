"""서빙 판정 — 계약과 승인 게이트를 하나의 결정으로 묶는다.

신선도나 품질 위반이 있을 때 낡은 값을 조용히 내보내지 않는다.
last-known-good을 유지하되 degraded 상태와 마지막 정상 시각을 함께
노출한다. 조용히 내보내면 소비자가 그것을 최신으로 오인하고, 그 오인은
금융에서 그대로 잘못된 산출로 이어진다.

네 가지 상태로 구분한다.

- FRESH:             계약 통과. 최신 데이터를 서빙한다
- APPROVED_OVERRIDE: 위반이 있으나 유효한 사람 승인이 있다. 최신을 서빙한다
- DEGRADED:          위반이 있고 승인이 없다. 직전 정상 스냅샷을 서빙하되
                     상태와 마지막 정상 시각을 함께 알린다
- BLOCKED:           위반이 있고 승인도, 직전 정상 스냅샷도 없다. 내보낼 것이 없다

승인된 데이터는 정상 스냅샷으로 승격된다. 따라서 나중에 승인이 만료되고
새 위반이 발생하면, degraded로 내보내는 last-known-good에는 그때 승인했던
값이 들어 있다. 이는 의도된 동작이다. 승인은 "이 값은 정당하다"는 사람의
판단이고, 만료는 "새로운 위반까지 자동으로 통과시키지는 않는다"는 뜻이지
과거 판단을 소급 무효화한다는 뜻이 아니다.

그 대가로 잘못된 승인은 기준선에 남는다. 이것이 승인 주체와 사유를
감사 로그에 반드시 남기는 이유다. 되돌리려면 사람이 개입해야 한다.
"""

import datetime as dt
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..collect.series import SeriesSpec
from ..contracts.quality import QualityFinding, QualityReport
from ..contracts.schema import Observation
from ..gate.ledger import ExceptionLedger


class ServingState(StrEnum):
    FRESH = "fresh"
    APPROVED_OVERRIDE = "approved_override"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Snapshot:
    series_id: str
    observations: tuple[Observation, ...]
    request_ids: tuple[str, ...]
    written_at: dt.datetime


@dataclass(frozen=True)
class ServingDecision:
    series_id: str
    state: ServingState
    observations: list[Observation]
    findings: list[QualityFinding]
    last_good_at: dt.datetime | None

    @property
    def is_current(self) -> bool:
        """서빙 중인 값이 이번 수집분인가. degraded면 아니다."""
        return self.state in (ServingState.FRESH, ServingState.APPROVED_OVERRIDE)


class ServingStore:
    """last-known-good 스냅샷 보관소. 재시작해도 남아야 한다."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, series_id: str) -> Path:
        return self._root / f"{series_id}.json"

    def save(self, snapshot: Snapshot) -> None:
        if not snapshot.observations:
            raise ValueError("cannot snapshot an empty observations list")
        payload = {
            "series_id": snapshot.series_id,
            "written_at": snapshot.written_at.isoformat(),
            "request_ids": list(snapshot.request_ids),
            "observations": [
                {
                    "period": obs.period.isoformat(),
                    "raw_period": obs.raw_period,
                    "value": obs.value,
                    "unit": obs.unit,
                    "request_id": obs.request_id,
                }
                for obs in snapshot.observations
            ],
        }
        self._path(snapshot.series_id).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def load(self, series_id: str) -> Snapshot | None:
        path = self._path(series_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        observations = tuple(
            Observation(
                series_id=payload["series_id"],
                period=dt.date.fromisoformat(row["period"]),
                raw_period=row["raw_period"],
                value=row["value"],
                unit=row["unit"],
                request_id=row["request_id"],
            )
            for row in payload["observations"]
        )
        return Snapshot(
            series_id=payload["series_id"],
            observations=observations,
            request_ids=tuple(payload["request_ids"]),
            written_at=dt.datetime.fromisoformat(payload["written_at"]),
        )


def _promote(
    spec: SeriesSpec, observations: list[Observation], store: ServingStore, now: dt.datetime
) -> None:
    if not observations:
        raise ValueError("cannot promote an empty observations list to a snapshot")
    store.save(
        Snapshot(
            series_id=spec.series_id,
            observations=tuple(observations),
            request_ids=tuple(dict.fromkeys(obs.request_id for obs in observations)),
            written_at=now,
        )
    )


def decide_serving(
    spec: SeriesSpec,
    report: QualityReport,
    observations: list[Observation],
    store: ServingStore,
    ledger: ExceptionLedger,
    now: dt.datetime,
) -> ServingDecision:
    if report.ok:
        _promote(spec, observations, store, now)
        return ServingDecision(spec.series_id, ServingState.FRESH, observations, [], now)

    if ledger.is_cleared(spec.series_id, now):
        # 사람이 근거를 보고 통과시켰다. 승인분은 정상 스냅샷으로 승격한다.
        _promote(spec, observations, store, now)
        return ServingDecision(
            spec.series_id, ServingState.APPROVED_OVERRIDE, observations, report.findings, now
        )

    previous = store.load(spec.series_id)
    if previous is None:
        return ServingDecision(spec.series_id, ServingState.BLOCKED, [], report.findings, None)

    # 위반한 값은 절대 내보내지 않는다. 직전 정상 스냅샷만 내보낸다.
    return ServingDecision(
        spec.series_id,
        ServingState.DEGRADED,
        list(previous.observations),
        report.findings,
        previous.written_at,
    )
