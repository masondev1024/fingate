"""Fault injection 실험 하네스.

같은 오염을 두 파이프라인에 통과시켜 서빙 도달률을 비교한다.

- 대조군(naive): HTTP 상태만 확인하고 파싱해서 그대로 서빙한다.
  실제로 흔히 쓰이는 방식이며, 두 API가 오류에도 HTTP 200을 주므로
  오류 페이로드를 데이터로 적재한다.
- 실험군(gated): 응답·스키마·품질 3계층 계약과 승인 게이트를 거친다.

"서빙 도달"은 오염된 값이 소비자에게 최신 값으로 노출되는 것을 뜻한다.
degraded로 직전 정상 스냅샷을 내보내는 것은 도달이 아니다.
"""

import datetime as dt
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..collect.series import SeriesSpec
from ..contracts.quality import check_quality
from ..contracts.response import verify_dart_json, verify_dart_zip, verify_ecos
from ..contracts.schema import normalize_ecos_rows
from ..gate.ledger import ExceptionLedger
from ..serve.snapshot import ServingState, ServingStore, decide_serving
from .catalog import CATALOG, Corruption, SourceKind


@dataclass(frozen=True)
class TrialResult:
    corruption_id: str
    naive_reached_serving: bool
    naive_outcome: str  # served_bad_data | crashed | rejected
    gated_reached_serving: bool
    blocked_by_layer: str | None
    detail: str


def _naive_pipeline(body: bytes, rows: list[dict], kind: SourceKind) -> tuple[bool, str]:
    """게이트 없는 파이프라인이 오염을 어떻게 처리하는가.

    HTTP 상태는 어차피 200이므로 확인해도 통과한다. 엔드포인트에 맞는
    파서를 쓰되 계약 검증은 하지 않는다.

    결과를 셋으로 구분한다. 조용히 나쁜 데이터를 서빙하는 것이 가장 위험하고,
    크래시는 최소한 눈에 띈다는 점에서 다르다.
    """
    if kind is SourceKind.DART_ZIP:
        try:
            zipfile.ZipFile(io.BytesIO(body)).namelist()
        except Exception:
            return False, "crashed"
        return True, "served_bad_data"

    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return False, "crashed"
    if not isinstance(parsed, dict):
        return False, "rejected"
    # 오류 본문도 딕셔너리이므로 파싱은 성공한다. 이것이 문제의 핵심이다.
    if rows or "RESULT" in parsed or "status" in parsed:
        return True, "served_bad_data"
    return False, "rejected"


def _gated_response(body: bytes, kind: SourceKind, min_zip_bytes: int) -> tuple[bool, str]:
    """엔드포인트에 맞는 응답 계약으로 검증한다.

    검증기를 잘못 물리면 차단은 되더라도 귀속이 틀린다.
    """
    if kind is SourceKind.ECOS_JSON:
        verdict = verify_ecos(body)
    elif kind is SourceKind.DART_JSON:
        verdict = verify_dart_json(body)
    else:
        verdict = verify_dart_zip(body, min_zip_bytes)
    return verdict.ok, verdict.code or ""


@dataclass
class Harness:
    spec: SeriesSpec
    clean_rows: list[dict]
    store_root: Path
    as_of: dt.date
    now: dt.datetime

    def _serve(self, rows: list[dict], tag: str) -> tuple[bool, str]:
        """스키마·품질·서빙까지 통과시켜 최신 값이 서빙되는지 본다."""
        result = normalize_ecos_rows(self.spec, rows, request_id=f"req-{tag}")
        if not result.observations:
            return False, "schema"

        report = check_quality(self.spec, result.observations, as_of=self.as_of)
        store = ServingStore(self.store_root / tag)
        ledger = ExceptionLedger(self.store_root / f"{tag}-audit.jsonl")
        decision = decide_serving(self.spec, report, result.observations, store, ledger, self.now)
        if decision.state is ServingState.FRESH:
            return True, ""
        return False, "quality" if report.findings else "schema"

    def run(self, corruption: Corruption, body: bytes, rows: list[dict]) -> TrialResult:
        naive, outcome = _naive_pipeline(body, rows, corruption.source_kind)

        response_ok, code = _gated_response(body, corruption.source_kind, min_zip_bytes=100_000)
        if not response_ok:
            return TrialResult(corruption.corruption_id, naive, outcome, False, "response", code)

        reached, layer = self._serve(rows, corruption.corruption_id)
        return TrialResult(
            corruption.corruption_id, naive, outcome, reached, None if reached else layer, ""
        )


def summarize(results: list[TrialResult]) -> dict[str, object]:
    total = len(results)
    naive = sum(1 for r in results if r.naive_reached_serving)
    gated = sum(1 for r in results if r.gated_reached_serving)
    outcomes: dict[str, int] = {}
    for result in results:
        outcomes[result.naive_outcome] = outcomes.get(result.naive_outcome, 0) + 1
    by_layer: dict[str, int] = {}
    for result in results:
        if result.blocked_by_layer:
            by_layer[result.blocked_by_layer] = by_layer.get(result.blocked_by_layer, 0) + 1
    return {
        "total": total,
        "naive_reached": naive,
        "gated_reached": gated,
        "naive_rate": naive / total if total else 0.0,
        "gated_rate": gated / total if total else 0.0,
        "blocked_by_layer": by_layer,
        "naive_outcomes": outcomes,
        "catalog_size": len(CATALOG),
    }
