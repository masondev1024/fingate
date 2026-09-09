import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from fingate.agent.grounding import check_grounding
from fingate.agent.session import Answer, ask
from fingate.agent.tools import Toolbox
from fingate.collect.raw_store import RawStore
from fingate.contracts.quality import QualityFinding
from fingate.contracts.schema import Observation
from fingate.gate.ledger import ExceptionLedger
from fingate.serve.snapshot import ServingStore
from fingate.warehouse.store import Warehouse

NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
SERIES = "base_rate_daily"
PEER = "call_rate_daily"
START = dt.date(2026, 1, 1)


# --- 가짜 클라이언트: 네트워크 없이 루프를 검증한다 -------------------------


@dataclass
class Text:
    text: str
    type: str = "text"


@dataclass
class ToolUse:
    name: str
    input: dict
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class Reply:
    content: list
    stop_reason: str = "end_turn"
    stop_details: Any = None


@dataclass
class FakeClient:
    """미리 정해 둔 응답을 순서대로 돌려준다."""

    replies: list
    seen: list = field(default_factory=list)

    def create(self, **kwargs):
        self.seen.append(kwargs)
        return self.replies.pop(0)


# --- 실제 구성 요소 위에 차단된 건 하나 --------------------------------------


@pytest.fixture
def toolbox(tmp_path):
    warehouse = Warehouse()
    subject, peer, level = [], [], 3.0
    for day in range(120):
        if day and day % 20 == 0:
            level += 0.25
        subject.append(level)
        peer.append(level + 0.5)
    subject.append(subject[-1] + 0.85)
    peer.append(peer[-1] + 0.80)
    suspect = START + dt.timedelta(days=len(subject) - 1)

    body = json.dumps(
        {
            "StatisticSearch": {
                "list_total_count": 1,
                "row": [{"TIME": suspect.strftime("%Y%m%d"), "DATA_VALUE": f"{subject[-1]}"}],
            }
        }
    )
    raw_store = RawStore(tmp_path / "raw")
    request_id = raw_store.put("ecos", "s", {}, body.encode("utf-8")).request_id

    for series_id, values in ((SERIES, subject), (PEER, peer)):
        warehouse.load_observations(
            [
                Observation(
                    series_id=series_id,
                    period=START + dt.timedelta(days=i),
                    raw_period=(START + dt.timedelta(days=i)).strftime("%Y%m%d"),
                    value=v,
                    unit="percent_per_annum",
                    request_id=request_id,
                )
                for i, v in enumerate(values)
            ],
            NOW,
        )

    ledger = ExceptionLedger(tmp_path / "audit.jsonl")
    staged = ledger.stage(
        SERIES,
        [QualityFinding(SERIES, "JUMP_EXCEEDED", "0.85 exceeds 0.75", suspect)],
        request_id,
        NOW,
        dt.timedelta(days=7),
    )

    box = Toolbox(
        ledger=ledger,
        warehouse=warehouse,
        raw_store=raw_store,
        serving=ServingStore(tmp_path / "serving"),
        now=NOW,
    )
    yield box, staged.exception_id
    warehouse.close()


def _ask(client, toolbox, exception_id, question="이 급변은 진짜인가?"):
    return ask(question, exception_id=exception_id, toolbox=toolbox, client=client)


# --- 루프 -------------------------------------------------------------------


def test_the_agent_calls_a_tool_then_answers(toolbox):
    box, exception_id = toolbox
    client = FakeClient(
        [
            Reply(
                [
                    ToolUse(
                        "read_evidence", {"exception_id": exception_id, "probe": "anchor_spread"}
                    )
                ],
                stop_reason="tool_use",
            ),
            Reply([Text("앵커와의 관계가 유지됐다.")]),
        ]
    )

    answer = _ask(client, box, exception_id)

    assert isinstance(answer, Answer)
    assert answer.released
    assert answer.tool_calls == [
        ("read_evidence", {"exception_id": exception_id, "probe": "anchor_spread"})
    ]


def test_tool_results_go_back_as_a_single_user_message(toolbox):
    """병렬 호출 결과를 쪼개 보내면 모델이 병렬 호출을 그만둔다."""
    box, exception_id = toolbox
    client = FakeClient(
        [
            Reply(
                [
                    ToolUse("get_exception", {"exception_id": exception_id}, id="a"),
                    ToolUse("read_recommendation", {"exception_id": exception_id}, id="b"),
                ],
                stop_reason="tool_use",
            ),
            Reply([Text("확인했다.")]),
        ]
    )

    _ask(client, box, exception_id)
    final_messages = client.seen[-1]["messages"]
    tool_result_messages = [
        m
        for m in final_messages
        if isinstance(m["content"], list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    ]

    assert len(tool_result_messages) == 1
    assert len(tool_result_messages[0]["content"]) == 2


def test_the_request_asks_for_adaptive_thinking_and_the_tools(toolbox):
    box, exception_id = toolbox
    client = FakeClient([Reply([Text("근거가 없다.")])])

    _ask(client, box, exception_id)
    request = client.seen[0]

    assert request["thinking"] == {"type": "adaptive"}
    assert {t["name"] for t in request["tools"]} >= {"read_evidence", "get_exception"}
    assert all(t["strict"] for t in request["tools"])


# --- 근거 고정: 이 계층의 존재 이유 ------------------------------------------


def test_an_answer_grounded_in_tool_results_is_released(toolbox):
    box, exception_id = toolbox
    client = FakeClient(
        [
            Reply(
                [
                    ToolUse(
                        "read_evidence",
                        {"exception_id": exception_id, "probe": "peer_corroboration"},
                    )
                ],
                stop_reason="tool_use",
            ),
            Reply([Text("call_rate_daily가 동행했다. 동행률은 1.0이다.")]),
        ]
    )

    answer = _ask(client, box, exception_id)

    assert answer.released, f"근거 없음으로 잡힌 수치: {answer.grounding.ungrounded}"


def test_a_fabricated_number_is_caught_and_the_answer_is_withheld(toolbox):
    """모델이 그럴듯한 금리를 지어내면 승인자에게 보여주지 않는다."""
    box, exception_id = toolbox
    client = FakeClient(
        [
            Reply(
                [
                    ToolUse(
                        "read_evidence", {"exception_id": exception_id, "probe": "anchor_spread"}
                    )
                ],
                stop_reason="tool_use",
            ),
            Reply([Text("금통위가 기준금리를 4.25로 인상했으므로 정상이다.")]),
        ]
    )

    answer = _ask(client, box, exception_id)

    assert not answer.released
    assert "4.25" in answer.grounding.ungrounded


def test_an_answer_with_no_tool_call_cannot_smuggle_numbers(toolbox):
    """도구를 안 부르고 숫자를 말하면 근거가 있을 수 없다."""
    box, exception_id = toolbox
    client = FakeClient([Reply([Text("기준금리는 3.50이다.")])])

    answer = _ask(client, box, exception_id)

    assert not answer.released
    assert "3.50" in answer.grounding.ungrounded


def test_a_number_the_approver_asked_about_is_allowed_but_labelled(toolbox):
    box, exception_id = toolbox
    client = FakeClient([Reply([Text("말씀하신 0.85 급변은 도구로 확인해야 한다.")])])

    answer = ask(
        "0.85 급변이 진짜인가?",
        exception_id=exception_id,
        toolbox=box,
        client=client,
    )

    assert answer.released
    assert answer.grounding.sources["0.85"] == "prompt"


# --- 안전 경계 ---------------------------------------------------------------


def test_a_refusal_is_surfaced_not_routed_to_another_model(toolbox):
    """조용한 모델 대체는 이 프로젝트가 반대하는 형태의 은폐다."""
    box, exception_id = toolbox
    client = FakeClient([Reply([], stop_reason="refusal")])

    answer = _ask(client, box, exception_id)

    assert answer.refused
    assert not answer.released


def test_the_loop_stops_instead_of_calling_tools_forever(toolbox):
    box, exception_id = toolbox
    client = FakeClient(
        [
            Reply(
                [ToolUse("get_exception", {"exception_id": exception_id})], stop_reason="tool_use"
            )
            for _ in range(50)
        ]
    )

    answer = ask("왜?", exception_id=exception_id, toolbox=box, client=client, max_turns=3)

    assert answer.refused
    assert len(answer.tool_calls) == 3


def test_the_agent_cannot_change_the_ledger(toolbox):
    """reviewer 와 같은 규율이다. 코드 규약이 아니라 실행으로 막는다."""
    box, exception_id = toolbox

    def forbidden(*args, **kwargs):
        raise AssertionError("에이전트가 원장을 변경하려 했다")

    box.ledger.approve = forbidden
    box.ledger.reject = forbidden
    box.ledger.revoke = forbidden
    box.ledger.stage = forbidden

    client = FakeClient(
        [
            Reply(
                [ToolUse("read_recommendation", {"exception_id": exception_id})],
                stop_reason="tool_use",
            ),
            Reply([Text("권고를 읽었다.")]),
        ]
    )
    answer = _ask(client, box, exception_id)

    assert answer.released
    assert box.ledger.get(exception_id).status == "pending"


def test_no_tool_can_approve_or_revoke():
    """도구 목록 자체에 원장을 바꾸는 수단이 없어야 한다."""
    from fingate.agent.tools import TOOL_SCHEMAS

    names = {tool["name"] for tool in TOOL_SCHEMAS}

    assert not any(
        word in name for name in names for word in ("approve", "reject", "revoke", "stage", "write")
    )


def test_an_unknown_exception_is_reported_as_a_tool_result_not_a_crash(toolbox):
    """모델에게 스택트레이스를 주면 그것을 근거처럼 인용한다."""
    box, _ = toolbox

    outcome = box.run("get_exception", {"exception_id": "does-not-exist"})

    assert "error" in outcome


def test_the_tool_result_carries_the_summary_not_only_the_facts(toolbox):
    """요약문이 도구 반환값에 포함되어야 그 문장을 인용해도 근거가 선다.

    facts 만 돌려주면, probe 가 스스로 쓴 문장을 모델이 그대로 옮겨도 그 안의
    시점이 근거 없음으로 잡힌다. 실측에서 이 한 가지가 오탐 7.7% 와 0% 를 갈랐다.
    """
    box, exception_id = toolbox

    outcome = box.run("read_evidence", {"exception_id": exception_id, "probe": "anchor_spread"})

    assert "summary" in outcome and outcome["summary"]
    report = check_grounding(outcome["summary"], tool_results=[outcome], prompt_text="")
    assert report.ok, f"probe 자신의 문장이 근거 없음으로 잡혔다: {report.ungrounded}"


# --- 공급자를 바꿔도 같은 루프, 같은 검증 -----------------------------------


def test_the_same_loop_runs_through_the_gemini_adapter(toolbox):
    """어댑터만 바뀌고 루프·도구·검증은 그대로다.

    이 분리가 없으면 "환각을 막았다"는 주장이 특정 모델의 성질에 기대게 된다.
    """
    pytest.importorskip("google.genai")
    from dataclasses import dataclass as dc

    from fingate.agent.gemini import GeminiMessages

    box, exception_id = toolbox

    @dc
    class Call:
        name: str
        args: dict
        id: str | None = None

    @dc
    class Part:
        text: str | None = None
        function_call: object = None
        thought: bool = False

    @dc
    class Cand:
        content: object
        finish_reason: str = "STOP"

    @dc
    class Wrap:
        parts: list

    class Models:
        def __init__(self, replies):
            self.replies = replies

        def generate_content(self, **kwargs):
            return self.replies.pop(0)

    class Genai:
        def __init__(self, models):
            self.models = models

    replies = [
        type(
            "R",
            (),
            {
                "candidates": [
                    Cand(
                        Wrap(
                            [
                                Part(
                                    function_call=Call(
                                        "read_evidence",
                                        {"exception_id": exception_id, "probe": "anchor_spread"},
                                    )
                                )
                            ]
                        )
                    )
                ]
            },
        )(),
        type("R", (), {"candidates": [Cand(Wrap([Part(text="기준금리를 4.25로 올렸다.")]))]})(),
    ]
    client = GeminiMessages(Genai(Models(replies)))

    answer = ask("진짜인가?", exception_id=exception_id, toolbox=box, client=client)

    assert answer.tool_calls[0][0] == "read_evidence"
    assert not answer.released, "공급자가 바뀌어도 지어낸 수치는 막혀야 한다"
    assert "4.25" in answer.grounding.ungrounded
