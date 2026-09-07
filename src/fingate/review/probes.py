"""근거 probe — 차단된 건에 대해 승인자가 판단할 재료를 조립한다.

각 probe는 판정 하나와 **그 판정의 근거가 된 숫자**를 함께 돌려준다. 판정만
주면 승인자는 그것을 믿거나 무시하는 것 말고 할 수 있는 게 없다. 숫자가 있어야
스스로 가중치를 정할 수 있다.

모두 결정론적이다. SQL과 파일 대조로만 구현하고 추론하지 않는다. 근거 수집이
환각하면 감사 로그가 오염되고, 나중에 왜 통과시켰는지 추적할 수 없게 된다.
감사가 필요한 시스템에서 근거는 재현 가능해야 한다.
"""

import datetime as dt
import json
from dataclasses import dataclass, field
from enum import StrEnum

from ..collect.raw_store import RawStore
from ..collect.series import SeriesSpec
from ..contracts.quality import QualityFinding
from ..contracts.schema import parse_period
from ..gate.ledger import ExceptionLedger, ExceptionStatus
from ..serve.snapshot import ServingStore
from ..warehouse.store import Warehouse
from .peers import MOVE_EPSILON, peers_for

# 서빙 뷰가 직접 소비하는 계열. 나머지는 막혀도 이 제품에 도달하지 않는다.
SERVING_RATE_COLUMNS = {
    "base_rate_daily": "base_rate",
    "ktb_3y_daily": "ktb_3y",
    "ktb_10y_daily": "ktb_10y",
}


class ProbeVerdict(StrEnum):
    SUPPORTS_REAL = "supports_real"
    SUPPORTS_DEFECT = "supports_defect"
    INSUFFICIENT = "insufficient"
    # 결정에 방향을 주지 않고 맥락만 제공한다. 권고 규칙이 읽지 않는다.
    CONTEXT = "context"


@dataclass(frozen=True)
class Evidence:
    probe: str
    verdict: ProbeVerdict
    summary: str
    facts: dict[str, object] = field(default_factory=dict)


def _change_at(warehouse: Warehouse, series_id: str, period: dt.date) -> float | None:
    rows = warehouse.query(
        """
        SELECT change FROM (
            SELECT series_id, period,
                   value - lag(value) OVER (PARTITION BY series_id ORDER BY period) AS change
            FROM bronze_rate_observation
        )
        WHERE series_id = ? AND period = ?
        """,
        (series_id, period),
    )
    return None if not rows or rows[0]["change"] is None else float(rows[0]["change"])


def peer_corroboration(warehouse: Warehouse, series_id: str, period: dt.date) -> Evidence:
    """연동 계열이 같은 날 같은 방향으로 움직였는가.

    자격 peer가 없으면 INSUFFICIENT다. SUPPORTS_DEFECT가 아니다. 근거가 없는
    것과 결함의 증거가 있는 것은 전혀 다르고, 이를 섞으면 정상 데이터를
    결함으로 몰게 된다.
    """
    links = peers_for(warehouse, series_id)
    subject_change = _change_at(warehouse, series_id, period)

    if subject_change is None:
        return Evidence(
            "peer_corroboration",
            ProbeVerdict.INSUFFICIENT,
            f"{period}의 변화량을 계산할 수 없다(직전 관측 없음).",
            {"subject_change": None, "peers": []},
        )
    if not links:
        return Evidence(
            "peer_corroboration",
            ProbeVerdict.INSUFFICIENT,
            f"{series_id}와 함께 움직이는 것으로 측정된 계열이 없다. 판단할 근거가 없다.",
            {"subject_change": subject_change, "peers": []},
        )

    observed: list[dict[str, object]] = []
    for link in links:
        peer_change = _change_at(warehouse, link.peer_id, period)
        material = (
            peer_change is not None
            and abs(peer_change) > max(link.noise_p90, MOVE_EPSILON)
            and peer_change * subject_change > 0
        )
        observed.append(
            {
                "peer_id": link.peer_id,
                "peer_change": peer_change,
                "agreement": link.agreement,
                "lift": link.lift,
                "move_samples": link.move_samples,
                "noise_p90": link.noise_p90,
                "co_moved": material,
            }
        )

    facts = {"subject_change": subject_change, "peers": observed}
    agreed = [entry for entry in observed if entry["co_moved"]]
    measured = [entry for entry in observed if entry["peer_change"] is not None]

    if agreed:
        names = ", ".join(str(entry["peer_id"]) for entry in agreed)
        return Evidence(
            "peer_corroboration",
            ProbeVerdict.SUPPORTS_REAL,
            f"{names}이(가) 같은 날 같은 방향으로 유의미하게 움직였다. 실제 변동으로 보인다.",
            facts,
        )
    if not measured:
        return Evidence(
            "peer_corroboration",
            ProbeVerdict.INSUFFICIENT,
            f"자격 peer는 있으나 {period} 관측이 없어 대조할 수 없다.",
            facts,
        )
    names = ", ".join(str(entry["peer_id"]) for entry in measured)
    return Evidence(
        "peer_corroboration",
        ProbeVerdict.SUPPORTS_DEFECT,
        f"{names}은(는) 같은 날 평시 수준을 넘게 움직이지 않았다. 이 계열만 튀었다.",
        facts,
    )


def raw_provenance(
    raw_store: RawStore,
    spec: SeriesSpec,
    period: dt.date,
    value: float,
    request_id: str,
) -> Evidence:
    """의심스러운 값이 보존된 원본 응답에 실제로 들어 있는가.

    두 결과의 정답이 정반대이므로 다른 probe보다 우선한다.

    - 원본에 있다: 출처가 그렇게 보냈다. 판단 대상은 "이 값이 현실인가"다.
    - 원본에 없다: 우리가 값을 만들어냈다. 승인 대상이 아니라 버그다.
    """
    try:
        body = raw_store.get_body(request_id)
    except KeyError:
        return Evidence(
            "raw_provenance",
            ProbeVerdict.INSUFFICIENT,
            f"원본 응답 {request_id}를 찾을 수 없어 대조할 수 없다.",
            {"request_id": request_id, "raw_value": None},
        )

    try:
        payload = json.loads(body.decode("utf-8"))
        rows = payload["StatisticSearch"]["row"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return Evidence(
            "raw_provenance",
            ProbeVerdict.INSUFFICIENT,
            "원본 응답이 예상한 ECOS 구조가 아니어서 대조할 수 없다.",
            {"request_id": request_id, "raw_value": None},
        )

    for row in rows:
        try:
            if parse_period(str(row.get("TIME", "")), spec.cycle) != period:
                continue
            raw_value = float(str(row["DATA_VALUE"]).strip())
        except (ValueError, KeyError):
            continue

        facts = {"request_id": request_id, "raw_value": raw_value, "stored_value": value}
        if raw_value == value:
            return Evidence(
                "raw_provenance",
                ProbeVerdict.CONTEXT,
                f"원본 응답에 {period} = {raw_value}가 그대로 있다. 출처가 보낸 값이다.",
                facts,
            )
        return Evidence(
            "raw_provenance",
            ProbeVerdict.SUPPORTS_DEFECT,
            f"원본은 {raw_value}인데 적재된 값은 {value}다. 파이프라인이 값을 바꿨다.",
            facts,
        )

    return Evidence(
        "raw_provenance",
        ProbeVerdict.SUPPORTS_DEFECT,
        f"원본 응답에 {period} 관측이 없다. 적재된 값의 출처가 없다.",
        {"request_id": request_id, "raw_value": None, "stored_value": value},
    )


def historical_precedent(
    warehouse: Warehouse, spec: SeriesSpec, finding: QualityFinding
) -> Evidence:
    """같은 임계를 넘은 적이 과거에 있었는가.

    전례가 있으면 임계가 보수적이라는 뜻일 수 있고, 한 번도 없으면 이례성이 크다.
    어느 쪽도 결정을 대신하지 않으므로 방향을 주지 않는다.
    """
    rows = warehouse.query(
        """
        SELECT period, abs(change) AS jump FROM (
            SELECT series_id, period,
                   value - lag(value) OVER (PARTITION BY series_id ORDER BY period) AS change
            FROM bronze_rate_observation
        )
        WHERE series_id = ? AND change IS NOT NULL AND abs(change) > ?
        ORDER BY jump DESC
        """,
        (spec.series_id, spec.max_jump),
    )
    largest = warehouse.query(
        """
        SELECT max(abs(change)) AS peak FROM (
            SELECT series_id,
                   value - lag(value) OVER (PARTITION BY series_id ORDER BY period) AS change
            FROM bronze_rate_observation
        )
        WHERE series_id = ?
        """,
        (spec.series_id,),
    )
    peak = largest[0]["peak"] if largest and largest[0]["peak"] is not None else None

    facts = {
        "rule": finding.rule,
        "threshold": spec.max_jump,
        "breach_count": len(rows),
        "max_observed_jump": float(peak) if peak is not None else None,
        "breach_periods": [str(row["period"]) for row in rows[:5]],
    }
    if not rows:
        summary = (
            f"적재된 이력에서 {spec.max_jump}를 넘은 변동은 한 번도 없었다. "
            f"관측된 최대 변동폭은 {peak}다."
        )
    else:
        dates = ", ".join(str(row["period"]) for row in rows[:5])
        summary = f"같은 임계를 넘은 전례가 {len(rows)}번 있다({dates}). 최대 {peak}."
    return Evidence("historical_precedent", ProbeVerdict.CONTEXT, summary, facts)


def blast_radius(
    warehouse: Warehouse, serving: ServingStore, series_id: str, now: dt.datetime
) -> Evidence:
    """반려하면 무엇이 깨지는가.

    승인의 위험만 보고 결정할 수 없다. 마지막 정상값이 90일 전이라면 degraded
    유지도 안전한 선택이 아니다. 반려의 비용을 함께 놓아야 판단이 성립한다.
    """
    column = SERVING_RATE_COLUMNS.get(series_id)
    affected = 0
    if column is not None:
        rows = warehouse.query(
            f"SELECT count(*) AS n FROM serving_insurer_rate_context WHERE {column} IS NOT NULL"  # noqa: S608
        )
        affected = int(rows[0]["n"])

    snapshot = serving.load(series_id)
    age = None if snapshot is None else (now - snapshot.written_at).days

    facts = {
        "serving_rows_affected": affected,
        "serving_column": column,
        "last_good_at": None if snapshot is None else snapshot.written_at.isoformat(),
        "last_good_age_days": age,
    }
    if snapshot is None:
        summary = (
            f"직전 정상 스냅샷이 없다. 반려하면 이 계열은 서빙할 값이 없어 "
            f"blocked가 된다. 영향 행 {affected}."
        )
    else:
        summary = f"반려하면 {age}일 전 스냅샷으로 degraded 서빙한다. 영향 행 {affected}."
    return Evidence("blast_radius", ProbeVerdict.CONTEXT, summary, facts)


def prior_decisions(ledger: ExceptionLedger, series_id: str, rules: tuple[str, ...]) -> Evidence:
    """같은 계열·같은 규칙으로 과거에 어떻게 결정했는가.

    일관성 없는 결정은 그 자체로 감사 지적 사항이다. 승인자가 과거 판단을
    볼 수 없으면 일관성을 지킬 방법이 없다.
    """
    wanted = set(rules)
    related = [
        entry
        for entry in ledger.list()
        if entry.series_id == series_id
        and wanted & {finding.rule for finding in entry.findings}
        and entry.status in (ExceptionStatus.APPROVED, ExceptionStatus.REJECTED)
    ]
    ordered = sorted(related, key=lambda entry: entry.decided_at or entry.created_at, reverse=True)
    approved = sum(1 for entry in ordered if entry.status is ExceptionStatus.APPROVED)
    rejected = sum(1 for entry in ordered if entry.status is ExceptionStatus.REJECTED)

    facts = {
        "approved": approved,
        "rejected": rejected,
        "decisions": [
            {
                "exception_id": entry.exception_id,
                "status": str(entry.status),
                "decided_by": entry.decided_by,
                "note": entry.decision_note,
                "decided_at": entry.decided_at.isoformat() if entry.decided_at else None,
            }
            for entry in ordered[:5]
        ],
    }
    if not ordered:
        summary = "같은 계열·규칙으로 결정된 전례가 없다."
    else:
        latest = ordered[0]
        summary = (
            f"전례 승인 {approved}건, 반려 {rejected}건. "
            f"가장 최근: {latest.status} by {latest.decided_by} — {latest.decision_note}"
        )
    return Evidence("prior_decisions", ProbeVerdict.CONTEXT, summary, facts)
