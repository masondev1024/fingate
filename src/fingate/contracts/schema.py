"""스키마 계약 — 원본 행이 선언된 시계열 정의와 맞는지 검증한다.

거부된 행은 버리지 않고 사유·기간·계보와 함께 남긴다. 조용히 버리면
결측이 어디서 생겼는지 추적할 수 없고, 하류에서 그 빈 구간을 정상으로
오인한다.

세 가지 위반이 특히 위험하다.

- ITEM_MISMATCH / STAT_MISMATCH: 요청한 것과 다른 시계열이 왔다는 뜻이다.
  값 자체는 멀쩡해 보이므로 값 검증만으로는 절대 잡히지 않는다.
- UNIT_MISMATCH: 단위 라벨이 바뀌었다. 값의 의미가 달라졌을 수 있다.
- VALUE_NOT_NUMERIC: ECOS는 결측을 빈 문자열로 보낸다.
"""

import datetime as dt
import re
from dataclasses import dataclass

from ..collect.series import SeriesSpec

REQUIRED_FIELDS = ("STAT_CODE", "ITEM_CODE1", "UNIT_NAME", "TIME", "DATA_VALUE")

_PERIOD_PATTERNS = {
    "D": re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
    "M": re.compile(r"^(\d{4})(\d{2})$"),
    "Q": re.compile(r"^(\d{4})Q([1-4])$"),
    "A": re.compile(r"^(\d{4})$"),
}


def parse_period(raw: str, cycle: str) -> dt.date:
    """주기에 맞는 기간 문자열을 기간 시작일로 바꾼다.

    주기별로 형식이 다르므로 형식이 곧 검증이다. 월별 자리에 일별 문자열이
    오면 다른 시계열이 섞인 것이다.
    """
    pattern = _PERIOD_PATTERNS.get(cycle)
    if pattern is None:
        raise ValueError(f"unsupported cycle for period parsing: {cycle}")
    match = pattern.match(raw or "")
    if match is None:
        raise ValueError(f"period {raw!r} does not match cycle {cycle}")
    if cycle == "D":
        year, month, day = (int(g) for g in match.groups())
    elif cycle == "M":
        year, month, day = int(match.group(1)), int(match.group(2)), 1
    elif cycle == "Q":
        year, month, day = int(match.group(1)), (int(match.group(2)) - 1) * 3 + 1, 1
    else:
        year, month, day = int(match.group(1)), 1, 1
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"period {raw!r} is not a valid date: {exc}") from exc


@dataclass(frozen=True)
class Observation:
    series_id: str
    period: dt.date
    raw_period: str
    value: float
    unit: str
    request_id: str


@dataclass(frozen=True)
class RejectedRow:
    series_id: str
    raw_period: str
    reason: str
    detail: str
    request_id: str


@dataclass(frozen=True)
class NormalizationResult:
    observations: list[Observation]
    rejected: list[RejectedRow]

    @property
    def acceptance_rate(self) -> float:
        total = len(self.observations) + len(self.rejected)
        return len(self.observations) / total if total else 0.0


def _check(spec: SeriesSpec, row: dict) -> tuple[str, str] | None:
    """위반이 있으면 (사유, 상세)를 돌려준다. 없으면 None."""
    missing = [field for field in REQUIRED_FIELDS if field not in row]
    if missing:
        return "MISSING_FIELD", f"missing fields: {missing}"
    if row["STAT_CODE"] != spec.stat_code:
        return "STAT_MISMATCH", f"expected {spec.stat_code}, got {row['STAT_CODE']}"
    if row["ITEM_CODE1"] != spec.item_code:
        return "ITEM_MISMATCH", f"expected {spec.item_code}, got {row['ITEM_CODE1']}"
    if row["UNIT_NAME"] != spec.source_unit:
        return "UNIT_MISMATCH", f"expected {spec.source_unit!r}, got {row['UNIT_NAME']!r}"
    return None


def normalize_ecos_rows(spec: SeriesSpec, rows: list[dict], request_id: str) -> NormalizationResult:
    observations: list[Observation] = []
    rejected: list[RejectedRow] = []

    for row in rows:
        raw_period = str(row.get("TIME", ""))

        violation = _check(spec, row)
        if violation is not None:
            reason, detail = violation
            rejected.append(RejectedRow(spec.series_id, raw_period, reason, detail, request_id))
            continue

        try:
            period = parse_period(raw_period, spec.cycle)
        except ValueError as exc:
            rejected.append(
                RejectedRow(spec.series_id, raw_period, "PERIOD_FORMAT", str(exc), request_id)
            )
            continue

        try:
            value = float(str(row["DATA_VALUE"]).strip())
        except ValueError:
            rejected.append(
                RejectedRow(
                    spec.series_id,
                    raw_period,
                    "VALUE_NOT_NUMERIC",
                    f"DATA_VALUE={row['DATA_VALUE']!r}",
                    request_id,
                )
            )
            continue

        observations.append(
            Observation(
                series_id=spec.series_id,
                period=period,
                raw_period=raw_period,
                value=value,
                unit=spec.unit,
                request_id=request_id,
            )
        )

    return NormalizationResult(observations=observations, rejected=rejected)
