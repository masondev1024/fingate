"""품질 계약 — 스키마를 통과한 관측이 서빙될 자격이 있는지 판정한다.

임계는 2015~2026 실측 분포에서 도출했다. 추측으로 정하면 정상 데이터를
거부하거나(거짓 양성) 이상치를 놓친다(거짓 음성).

관측이 하나도 없는 것을 통과로 처리하지 않는다. 빈 결과를 정상으로 넘기면
빈 서빙이 정상이 되고, 그때 아무도 알아채지 못한다.
"""

import datetime as dt
from collections import Counter
from dataclasses import dataclass

from ..collect.series import SeriesSpec
from .schema import Observation


@dataclass(frozen=True)
class QualityFinding:
    series_id: str
    rule: str
    detail: str
    period: dt.date | None


@dataclass(frozen=True)
class QualityReport:
    series_id: str
    findings: list[QualityFinding]
    observed_count: int
    latest_period: dt.date | None

    @property
    def ok(self) -> bool:
        return not self.findings


def check_quality(
    spec: SeriesSpec, observations: list[Observation], as_of: dt.date
) -> QualityReport:
    wrong = {obs.series_id for obs in observations} - {spec.series_id}
    if wrong:
        raise ValueError(f"observations from another series: {sorted(wrong)}")

    findings: list[QualityFinding] = []

    def add(rule: str, detail: str, period: dt.date | None = None) -> None:
        findings.append(QualityFinding(spec.series_id, rule, detail, period))

    if not observations:
        add("NO_OBSERVATIONS", "series produced no observations")
        return QualityReport(spec.series_id, findings, 0, None)

    ordered = sorted(observations, key=lambda obs: obs.period)
    latest = ordered[-1].period

    for period, count in sorted(Counter(obs.period for obs in ordered).items()):
        if count > 1:
            add("DUPLICATE_PERIOD", f"period appears {count} times", period)

    for obs in ordered:
        if not spec.min_value <= obs.value <= spec.max_value:
            add(
                "VALUE_OUT_OF_RANGE",
                f"{obs.value} outside [{spec.min_value}, {spec.max_value}]",
                obs.period,
            )

    for previous, current in zip(ordered, ordered[1:], strict=False):
        jump = abs(current.value - previous.value)
        if jump > spec.max_jump:
            add(
                "JUMP_EXCEEDED",
                f"{jump:.3f} exceeds {spec.max_jump} between "
                f"{previous.period} and {current.period}",
                current.period,
            )

    staleness_days = (as_of - latest).days
    if staleness_days > spec.freshness_sla_days:
        add(
            "STALE",
            f"latest observation is {staleness_days} days old, SLA is {spec.freshness_sla_days}",
            latest,
        )

    return QualityReport(spec.series_id, findings, len(ordered), latest)
