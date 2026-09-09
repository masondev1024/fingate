"""도구 노출 — 에이전트가 무엇을 읽을 수 있는가.

근거 자체는 여전히 결정론적으로 조립된다(`review.assemble`). 에이전트가 하는
일은 **무엇을 읽을지 고르고, 읽은 것을 승인자의 질문에 맞게 설명하는 것**이다.
근거의 진위는 에이전트에 의존하지 않는다.

그래서 도구 이름이 `read_evidence` 이지 `run_probe` 가 아니다. probe 6종은
어차피 함께 계산된다 — 전부 작은 DuckDB 질의와 파일 대조라 값이 싸고, 하나만
따로 도는 경로를 두면 금리·재무 라우팅이 두 벌이 된다. 에이전트는 계산을
고르는 것이 아니라 **읽을 것을 고른다.** 이름이 그 사실과 어긋나면 안 된다.

도구 스키마에는 `strict` 를 건다. 모델이 보낸 인자가 스키마를 벗어나면 애초에
호출이 성립하지 않는다.
"""

import datetime as dt
import json
from dataclasses import dataclass

from ..collect.raw_store import RawStore
from ..gate.ledger import ExceptionLedger, ExceptionStatus
from ..review.assemble import Review, review_exception
from ..serve.snapshot import ServingStore
from ..warehouse.store import Warehouse

PROBE_NAMES = (
    "peer_corroboration",
    "anchor_spread",
    "raw_provenance",
    "financial_provenance",
    "historical_precedent",
    "financial_precedent",
    "blast_radius",
    "prior_decisions",
)

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_exceptions",
        "description": (
            "승인 대기 중이거나 이미 결정된 차단 건의 목록을 돌려준다. "
            "특정 건을 지목받지 않았을 때 무엇이 있는지 먼저 확인하는 용도다."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [str(value) for value in ExceptionStatus],
                    "description": "조회할 상태.",
                }
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_exception",
        "description": (
            "차단된 건 하나의 상세를 돌려준다. 어떤 계열이 어떤 규칙을 어겼고, "
            "언제 만료되며, 이미 결정됐다면 누가 무슨 사유로 결정했는지."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"exception_id": {"type": "string"}},
            "required": ["exception_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_evidence",
        "description": (
            "차단된 건에 대해 결정론적으로 조립된 근거 하나를 읽는다. "
            "판정과 함께 그 판정의 근거가 된 측정값을 돌려준다. "
            "여기서 돌려주지 않은 수치는 답변에 쓸 수 없다."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "exception_id": {"type": "string"},
                "probe": {
                    "type": "string",
                    "enum": list(PROBE_NAMES),
                    "description": (
                        "peer_corroboration: 연동 계열이 같은 날 같은 방향으로 움직였나. "
                        "anchor_spread: 앵커와의 수준 관계가 유지됐나. "
                        "raw_provenance / financial_provenance: 적재된 값이 원본과 일치하나. "
                        "historical_precedent / financial_precedent: 같은 임계를 넘은 전례가 있나. "
                        "blast_radius: 반려하면 서빙 몇 행이 영향받나. "
                        "prior_decisions: 같은 계열·규칙을 과거에 어떻게 결정했나."
                    ),
                },
            },
            "required": ["exception_id", "probe"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_recommendation",
        "description": (
            "결정론적 규칙이 근거들을 합쳐 낸 권고를 읽는다. "
            "권고는 제안이며 승인이 아니다. 이 도구도 원장을 바꾸지 않는다."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"exception_id": {"type": "string"}},
            "required": ["exception_id"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class Toolbox:
    """도구 실행기. 원장 쓰기 경로에는 접근하지 않는다.

    reviewer 와 같은 규율이다 — 에이전트는 승인·반려·철회를 실행할 수 없다.
    코드 규약이 아니라 테스트로 강제한다.
    """

    ledger: ExceptionLedger
    warehouse: Warehouse
    raw_store: RawStore
    serving: ServingStore
    now: dt.datetime

    def __post_init__(self) -> None:
        self._reviews: dict[str, Review] = {}

    def _review(self, exception_id: str) -> Review:
        if exception_id not in self._reviews:
            self._reviews[exception_id] = review_exception(
                exception_id,
                ledger=self.ledger,
                warehouse=self.warehouse,
                raw_store=self.raw_store,
                serving=self.serving,
                now=self.now,
            )
        return self._reviews[exception_id]

    def run(self, name: str, arguments: dict) -> dict:
        """도구를 실행하고 결과를 돌려준다. 실패는 예외가 아니라 결과로 표현한다.

        모델에게 스택트레이스를 주면 그것을 근거처럼 인용한다. 무엇이 없어서
        답할 수 없는지를 문장으로 돌려주는 편이 낫다.
        """
        try:
            if name == "list_exceptions":
                status = ExceptionStatus(arguments["status"])
                return {
                    "status": str(status),
                    "exceptions": [
                        {
                            "exception_id": entry.exception_id,
                            "series_id": entry.series_id,
                            "rules": sorted({f.rule for f in entry.findings}),
                            "expires_at": entry.expires_at.isoformat(),
                        }
                        for entry in self.ledger.list(status=status)
                    ],
                }

            if name == "get_exception":
                staged = self.ledger.get(arguments["exception_id"])
                return {
                    "exception_id": staged.exception_id,
                    "series_id": staged.series_id,
                    "status": str(staged.status),
                    "expires_at": staged.expires_at.isoformat(),
                    "findings": [
                        {
                            "rule": finding.rule,
                            "detail": finding.detail,
                            "period": finding.period.isoformat() if finding.period else None,
                        }
                        for finding in staged.findings
                    ],
                    "decided_by": staged.decided_by,
                    "decision_note": staged.decision_note,
                    "saw_recommendation": staged.saw_recommendation,
                    "revoked_by": staged.revoked_by,
                    "revocation_note": staged.revocation_note,
                }

            if name == "read_evidence":
                review = self._review(arguments["exception_id"])
                wanted = arguments["probe"]
                for item in review.evidence:
                    if item.probe == wanted:
                        return {
                            "probe": item.probe,
                            "verdict": str(item.verdict),
                            "summary": item.summary,
                            "facts": item.facts,
                        }
                return {
                    "probe": wanted,
                    "verdict": "not_applicable",
                    "summary": f"{wanted}는 이 건에 적용되지 않는다.",
                    "facts": {},
                }

            if name == "read_recommendation":
                review = self._review(arguments["exception_id"])
                return {
                    "verdict": str(review.recommendation.verdict),
                    "because": review.recommendation.because,
                }

            return {"error": f"unknown tool: {name}"}
        except KeyError as error:
            return {"error": str(error).strip("'")}


def render(result: dict) -> str:
    """도구 결과를 모델에게 보낼 문자열로 만든다. 근거 검증도 같은 값을 본다."""
    return json.dumps(result, ensure_ascii=False, default=str)
