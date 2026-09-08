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
from ..contracts.financial_quality import (
    DART_PREFIX,
    check_financial_quality,
    financial_series_id,
)
from ..contracts.quality import QualityFinding
from ..contracts.schema import parse_period
from ..gate.ledger import ExceptionLedger, ExceptionStatus
from ..serve.snapshot import ServingStore
from ..warehouse.indicators import resolve_indicators
from ..warehouse.store import Warehouse
from .anchors import anchors_for
from .peers import MOVE_EPSILON, peers_for


def corp_code_of(series_id: str) -> str | None:
    """DART 계열 식별자에서 회사 코드를 꺼낸다. 금리 계열이면 None."""
    prefix = f"{DART_PREFIX}:"
    return series_id[len(prefix) :] if series_id.startswith(prefix) else None


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
    links = peers_for(warehouse, series_id, exclude_period=period)
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
    corp_code = corp_code_of(series_id)
    column = SERVING_RATE_COLUMNS.get(series_id)
    affected = 0
    if corp_code is not None:
        # 재무는 서빙 뷰의 구동 테이블이다. 막으면 그 회사 행 자체가 사라진다.
        rows = warehouse.query(
            "SELECT count(*) AS n FROM serving_insurer_rate_context WHERE corp_code = ?",
            (corp_code,),
        )
        affected = int(rows[0]["n"])
    elif column is not None:
        # 금리는 LEFT JOIN 되는 맥락이다. 막아도 행은 남고 열이 빈다.
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


def financial_provenance(
    raw_store: RawStore,
    corp_code: str,
    bsns_year: int,
    report_code: str,
    findings: list[QualityFinding],
    request_id: str,
    *,
    previous_total_assets: int | None = None,
) -> Evidence:
    """보존된 원본을 다시 해석해 같은 위반이 재현되는지 본다.

    금리는 값 하나를 대조하면 되지만, 재무의 위반은 항등식·부호·결측이라
    단일 값 비교로는 답이 나오지 않는다. 대신 원본에서 지표를 다시 해석하고
    같은 계약을 다시 적용한다. 질문은 동일하다 — 이 위반이 출처의 것인가,
    우리가 만든 것인가.

    - 재현된다: 출처가 그렇게 보냈다. 판단 대상은 "이 수치가 현실인가"다.
    - 재현되지 않는다: 원본은 정합한데 우리 쪽만 깨졌다. 승인 대상이 아니라 버그다.
    """
    series_id = financial_series_id(corp_code)
    observed = sorted({finding.rule for finding in findings})

    try:
        body = raw_store.get_body(request_id)
    except KeyError:
        return Evidence(
            "financial_provenance",
            ProbeVerdict.INSUFFICIENT,
            f"원본 응답 {request_id}를 찾을 수 없어 대조할 수 없다.",
            {"request_id": request_id, "raw_rules": None, "observed_rules": observed},
        )

    try:
        payload = json.loads(body.decode("utf-8"))
        rows = payload["list"]
        if not isinstance(rows, list):
            raise TypeError
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return Evidence(
            "financial_provenance",
            ProbeVerdict.INSUFFICIENT,
            "원본 응답이 예상한 DART 구조가 아니어서 대조할 수 없다.",
            {"request_id": request_id, "raw_rules": None, "observed_rules": observed},
        )

    resolved, unmapped = resolve_indicators(report_code, rows)
    replayed = check_financial_quality(
        corp_code,
        bsns_year,
        report_code,
        resolved,
        previous_total_assets=previous_total_assets,
    )
    raw_rules = sorted({finding.rule for finding in replayed.findings})

    facts = {
        "request_id": request_id,
        "series_id": series_id,
        "raw_rules": raw_rules,
        "observed_rules": observed,
        "raw_indicators": len(resolved),
        "raw_unmapped": len(unmapped),
    }

    reproduced = [rule for rule in observed if rule in raw_rules]
    if reproduced:
        return Evidence(
            "financial_provenance",
            ProbeVerdict.CONTEXT,
            f"원본을 다시 해석해도 {', '.join(reproduced)}가 재현된다. 출처가 보낸 값이다.",
            facts,
        )
    if not raw_rules:
        return Evidence(
            "financial_provenance",
            ProbeVerdict.SUPPORTS_DEFECT,
            "원본은 계약을 통과한다. 적재 과정에서 위반이 생겼다.",
            facts,
        )
    return Evidence(
        "financial_provenance",
        ProbeVerdict.SUPPORTS_DEFECT,
        f"원본에서는 {', '.join(raw_rules)}가 나오고 기록된 위반과 다르다. "
        "적재 과정이 위반의 내용을 바꿨다.",
        facts,
    )


def financial_precedent(warehouse: Warehouse, corp_code: str) -> Evidence:
    """이 회사의 적재 이력. 총자산이 실제로 얼마나 움직여 왔는가.

    금리의 historical_precedent와 같은 역할이되 대상 테이블이 다르다.
    금리 테이블을 조회하면 재무 위반에 대해 항상 "전례 없음"이 나온다.
    """
    rows = warehouse.query(
        """
        SELECT count(*) AS quarters,
               max(abs(change)) AS max_change
        FROM (
            SELECT amount,
                   (amount - lag(amount) OVER (ORDER BY bsns_year * 10 + quarter))
                   / CAST(lag(amount) OVER (ORDER BY bsns_year * 10 + quarter) AS DOUBLE)
                       AS change
            FROM bronze_financial_indicator
            WHERE corp_code = ? AND indicator_id = 'total_assets'
        )
        """,
        (corp_code,),
    )
    quarters = int(rows[0]["quarters"]) if rows else 0
    peak = rows[0]["max_change"] if rows else None
    peak = float(peak) if peak is not None else None

    facts = {
        "corp_code": corp_code,
        "loaded_quarters": quarters,
        "max_asset_change": peak,
    }
    if quarters == 0:
        summary = "이 회사의 적재 이력이 없다. 비교할 전례가 없다."
    elif peak is None:
        summary = f"적재된 분기 {quarters}개. 변화를 계산할 직전 분기가 없다."
    else:
        summary = f"적재된 분기 {quarters}개, 관측된 총자산 최대 분기 변동 {peak:+.1%}."
    return Evidence("financial_precedent", ProbeVerdict.CONTEXT, summary, facts)


def anchor_spread(warehouse: Warehouse, spec: SeriesSpec, period: dt.date) -> Evidence:
    """대상이 앵커의 수준에서 얼마나 벗어났는가.

    `peer_corroboration` 은 앵커가 **움직인 날**을 조건으로 하므로, 거의 움직이지
    않는 계열은 근거가 되지 못한다. 실측에서 기준금리는 899일 중 892일 평평했고,
    그래서 `call_rate_daily` 에는 자격 peer 가 하나도 없었다.

    스프레드는 관계가 **유지되기만** 하면 된다. 앵커가 따라 움직여 스프레드가
    보전되면 실제 변동이고, 대상만 튀어 관계가 깨지면 그 계열의 결함이다.

    경보 임계는 쌍마다 관측된 꼬리에서 유도한다. 교과서 3σ 를 쓰면 실측 콜금리
    스프레드에서 정상 데이터 900일 중 11일을 오탐한다.
    """
    links = anchors_for(warehouse, spec.series_id, spec.max_jump, exclude_period=period)
    if not links:
        return Evidence(
            "anchor_spread",
            ProbeVerdict.INSUFFICIENT,
            f"{spec.series_id}의 수준을 규정하는 것으로 측정된 앵커가 없다.",
            {"anchors": []},
        )

    observed: list[dict[str, object]] = []
    for link in links:
        rows = warehouse.query(
            """
            SELECT subject.value - anchor.value AS spread
            FROM bronze_rate_observation AS subject
            JOIN bronze_rate_observation AS anchor ON anchor.period = subject.period
            WHERE subject.series_id = ? AND anchor.series_id = ? AND subject.period = ?
            """,
            (spec.series_id, link.anchor_id, period),
        )
        spread = float(rows[0]["spread"]) if rows else None
        z = None if spread is None else abs(spread - link.spread_mean) / link.spread_sd
        observed.append(
            {
                "anchor_id": link.anchor_id,
                "spread": spread,
                "spread_mean": link.spread_mean,
                "spread_sd": link.spread_sd,
                "z": z,
                "alert_z": link.alert_z,
                "observed_max_z": link.observed_max_z,
                "overlap": link.overlap,
                "broken": z is not None and z > link.alert_z,
            }
        )

    facts = {"anchors": observed}
    measured = [entry for entry in observed if entry["z"] is not None]
    broken = [entry for entry in measured if entry["broken"]]

    if not measured:
        return Evidence(
            "anchor_spread",
            ProbeVerdict.INSUFFICIENT,
            f"앵커는 있으나 {period} 관측이 없어 스프레드를 계산할 수 없다.",
            facts,
        )
    if broken:
        worst = max(broken, key=lambda entry: float(entry["z"]))
        return Evidence(
            "anchor_spread",
            ProbeVerdict.SUPPORTS_DEFECT,
            f"{worst['anchor_id']}와의 스프레드가 {float(worst['z']):.1f}σ 벗어났다"
            f"(경보 {float(worst['alert_z']):.1f}σ, 정상 최대 "
            f"{float(worst['observed_max_z']):.1f}σ). 수준 관계가 깨졌다.",
            facts,
        )
    closest = max(measured, key=lambda entry: float(entry["z"]))
    return Evidence(
        "anchor_spread",
        ProbeVerdict.SUPPORTS_REAL,
        f"{closest['anchor_id']}와의 스프레드가 {float(closest['z']):.1f}σ로 유지됐다"
        f"(경보 {float(closest['alert_z']):.1f}σ). 앵커가 함께 움직였다.",
        facts,
    )
