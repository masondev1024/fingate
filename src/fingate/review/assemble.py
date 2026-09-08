"""근거 조립 — 차단된 건 하나에 대해 probe를 모두 돌리고 권고를 붙인다.

여기가 "에이전트"에 해당하는 부분이다. 도구(probe)를 호출해 근거를 모으고
제안하지만, 실행 권한은 없다. 원장 쓰기 경로에 접근하지 않는다.

이미 결정된 건도 조회할 수 있다. 사후 감사가 목적이다. 과거 승인이 지금 기준으로
어떻게 보이는지 확인할 수 없으면 감사가 성립하지 않는다.
"""

import datetime as dt
from dataclasses import dataclass

from ..collect.raw_store import RawStore
from ..collect.series import series_by_id
from ..contracts.financial_quality import report_code_of
from ..gate.ledger import ExceptionLedger, StagedException
from ..serve.snapshot import ServingStore
from ..warehouse.store import Warehouse
from .probes import (
    Evidence,
    ProbeVerdict,
    blast_radius,
    corp_code_of,
    financial_precedent,
    financial_provenance,
    historical_precedent,
    peer_corroboration,
    prior_decisions,
    raw_provenance,
)
from .recommend import Recommended, recommend


@dataclass(frozen=True)
class Review:
    exception: StagedException
    evidence: list[Evidence]
    recommendation: Recommended


def _stored_value(warehouse: Warehouse, series_id: str, period: dt.date) -> float | None:
    rows = warehouse.query(
        "SELECT value FROM bronze_rate_observation WHERE series_id = ? AND period = ?",
        (series_id, period),
    )
    return float(rows[0]["value"]) if rows else None


def review_exception(
    exception_id: str,
    *,
    ledger: ExceptionLedger,
    warehouse: Warehouse,
    raw_store: RawStore,
    serving: ServingStore,
    now: dt.datetime,
) -> Review:
    staged = ledger.get(exception_id)
    rules = tuple(finding.rule for finding in staged.findings)
    corp_code = corp_code_of(staged.series_id)
    if corp_code is not None:
        return _review_financial(
            staged, corp_code, rules, ledger, warehouse, raw_store, serving, now
        )

    spec = series_by_id(staged.series_id)

    # 시점이 있는 위반만 관측 단위로 대조할 수 있다. NO_OBSERVATIONS처럼
    # 시점이 없는 위반은 대조할 관측 자체가 없다.
    periods = [finding.period for finding in staged.findings if finding.period is not None]
    suspect = max(periods) if periods else None

    evidence: list[Evidence] = []

    if suspect is None:
        evidence.append(
            Evidence(
                "peer_corroboration",
                ProbeVerdict.INSUFFICIENT,
                "위반에 시점이 없어 대조할 관측을 특정할 수 없다.",
                {"subject_change": None, "peers": []},
            )
        )
        evidence.append(
            Evidence(
                "raw_provenance",
                ProbeVerdict.INSUFFICIENT,
                "위반에 시점이 없어 원본과 대조할 행을 특정할 수 없다.",
                {"request_id": staged.request_id, "raw_value": None},
            )
        )
    else:
        evidence.append(peer_corroboration(warehouse, staged.series_id, suspect))
        value = _stored_value(warehouse, staged.series_id, suspect)
        if value is None:
            evidence.append(
                Evidence(
                    "raw_provenance",
                    ProbeVerdict.INSUFFICIENT,
                    f"{suspect} 관측이 적재되어 있지 않아 원본과 대조할 수 없다.",
                    {"request_id": staged.request_id, "raw_value": None},
                )
            )
        else:
            evidence.append(raw_provenance(raw_store, spec, suspect, value, staged.request_id))

    evidence.append(historical_precedent(warehouse, spec, staged.findings[0]))
    evidence.append(blast_radius(warehouse, serving, staged.series_id, now))
    evidence.append(prior_decisions(ledger, staged.series_id, rules))

    return Review(exception=staged, evidence=evidence, recommendation=recommend(evidence))


def _review_financial(
    staged: StagedException,
    corp_code: str,
    rules: tuple[str, ...],
    ledger: ExceptionLedger,
    warehouse: Warehouse,
    raw_store: RawStore,
    serving: ServingStore,
    now: dt.datetime,
) -> Review:
    """재무 위반은 다른 probe 를 쓴다.

    금리용 probe 를 그대로 쓰면 원본 구조가 달라 항상 대조 실패하고, 전례도
    금리 테이블을 조회해 언제나 "없음"이 나온다. 근거가 없는 것처럼 보이지만
    실제로는 잘못된 곳을 보고 있는 것이다. 둘은 구분되어야 한다.

    연동 계열 개념은 재무에 적용되지 않는다. 없는 근거를 만들지 않고
    적용 불가임을 명시한다.
    """
    periods = [finding.period for finding in staged.findings if finding.period is not None]
    suspect = max(periods) if periods else None

    evidence: list[Evidence] = [
        Evidence(
            "peer_corroboration",
            ProbeVerdict.INSUFFICIENT,
            "연동 계열 대조는 금리 계열에만 적용된다. 재무 지표에는 해당 개념이 없다.",
            {"subject_change": None, "peers": []},
        )
    ]

    if suspect is None:
        evidence.append(
            Evidence(
                "financial_provenance",
                ProbeVerdict.INSUFFICIENT,
                "위반에 시점이 없어 어느 보고서를 대조할지 특정할 수 없다.",
                {"request_id": staged.request_id, "raw_rules": None},
            )
        )
    else:
        try:
            report_code = report_code_of(suspect)
        except ValueError:
            report_code = None

        if report_code is None:
            evidence.append(
                Evidence(
                    "financial_provenance",
                    ProbeVerdict.INSUFFICIENT,
                    f"{suspect}가 분기말이 아니어서 보고서를 특정할 수 없다.",
                    {"request_id": staged.request_id, "raw_rules": None},
                )
            )
        else:
            evidence.append(
                financial_provenance(
                    raw_store,
                    corp_code,
                    suspect.year,
                    report_code,
                    list(staged.findings),
                    staged.request_id,
                    previous_total_assets=warehouse.previous_total_assets(
                        corp_code, suspect.year, _REPORT_QUARTER_OF_MONTH[suspect.month]
                    ),
                )
            )

    evidence.append(financial_precedent(warehouse, corp_code))
    evidence.append(blast_radius(warehouse, serving, staged.series_id, now))
    evidence.append(prior_decisions(ledger, staged.series_id, rules))

    return Review(exception=staged, evidence=evidence, recommendation=recommend(evidence))


_REPORT_QUARTER_OF_MONTH = {3: 1, 6: 2, 9: 3, 12: 4}
