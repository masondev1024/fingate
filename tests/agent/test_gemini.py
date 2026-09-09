import json
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("google.genai")

from fingate.agent.blocks import TextBlock, ToolUseBlock  # noqa: E402
from fingate.agent.gemini import GeminiMessages, _contents, _reply_of  # noqa: E402
from fingate.agent.tools import TOOL_SCHEMAS  # noqa: E402


@dataclass
class Call:
    name: str
    args: dict
    id: str | None = None


@dataclass
class Part:
    text: str | None = None
    function_call: Any = None
    thought: bool = False


@dataclass
class Content:
    parts: list


@dataclass
class Candidate:
    content: Any
    finish_reason: str = "STOP"


@dataclass
class Response:
    candidates: list


@dataclass
class FakeModels:
    reply: Any
    seen: list = field(default_factory=list)

    def generate_content(self, **kwargs):
        self.seen.append(kwargs)
        return self.reply


@dataclass
class FakeGenai:
    models: FakeModels


# --- 응답 변환 ---------------------------------------------------------------


def test_a_text_answer_becomes_an_end_turn_reply():
    reply = _reply_of(Response([Candidate(Content([Part(text="근거가 유지됐다.")]))]))

    assert reply.stop_reason == "end_turn"
    assert reply.content == [TextBlock(text="근거가 유지됐다.")]


def test_a_function_call_becomes_a_tool_use_block():
    call = Call(name="read_evidence", args={"exception_id": "x", "probe": "anchor_spread"})
    reply = _reply_of(Response([Candidate(Content([Part(function_call=call)]))]))

    assert reply.stop_reason == "tool_use"
    block = reply.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.name == "read_evidence"
    assert block.input == {"exception_id": "x", "probe": "anchor_spread"}
    assert block.id, "루프가 tool_result 를 붙이려면 id 가 있어야 한다"


def test_a_thinking_part_is_not_treated_as_the_answer():
    """사고 과정을 답변 본문으로 넘기면 그 안의 수치까지 근거 검증 대상이 된다."""
    parts = [Part(text="내부 추론", thought=True), Part(text="실제 답변")]

    reply = _reply_of(Response([Candidate(Content(parts))]))

    assert reply.content == [TextBlock(text="실제 답변")]


def test_a_safety_block_is_a_refusal_not_an_empty_answer():
    """빈 답변으로 넘기면 '근거 0개, 검증 통과' 가 되어 조용히 성공한다."""
    reply = _reply_of(Response([Candidate(Content([]), finish_reason="SAFETY")]))

    assert reply.stop_reason == "refusal"


def test_no_candidates_is_a_refusal():
    assert _reply_of(Response([])).stop_reason == "refusal"


# --- 대화 변환 ---------------------------------------------------------------


def test_the_assistant_role_is_renamed_for_gemini():
    converted = _contents([{"role": "assistant", "content": [TextBlock(text="답")]}])

    assert converted[0].role == "model"


def test_a_tool_result_is_matched_back_to_its_function_name():
    """Gemini 는 호출 id 가 아니라 함수 이름을 요구한다. 대응을 잃으면 붙지 않는다."""
    messages = [
        {"role": "user", "content": "질문"},
        {"role": "assistant", "content": [ToolUseBlock(name="read_evidence", input={}, id="tu_9")]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_9",
                    "content": json.dumps({"verdict": "ok"}),
                }
            ],
        },
    ]

    converted = _contents(messages)
    response_part = converted[-1].parts[0]

    assert response_part.function_response.name == "read_evidence"
    assert response_part.function_response.response == {"verdict": "ok"}


def test_a_non_json_tool_result_still_converts():
    messages = [
        {"role": "assistant", "content": [ToolUseBlock(name="t", input={}, id="i")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "i", "content": "평문"}],
        },
    ]

    converted = _contents(messages)

    assert converted[-1].parts[0].function_response.response == {"result": "평문"}


# --- 요청 구성 ---------------------------------------------------------------


def test_automatic_function_calling_is_disabled():
    """켜져 있으면 도구 반환값이 SDK 안에서 소비되어 근거 검증이 불가능해진다."""
    models = FakeModels(Response([Candidate(Content([Part(text="답")]))]))
    client = GeminiMessages(FakeGenai(models))

    client.create(
        model="claude-opus-5",
        max_tokens=16000,
        system="지침",
        tools=TOOL_SCHEMAS,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": "질문"}],
    )
    config = models.seen[0]["config"]

    assert config.automatic_function_calling.disable is True


def test_the_existing_tool_schemas_are_passed_through():
    models = FakeModels(Response([Candidate(Content([Part(text="답")]))]))
    client = GeminiMessages(FakeGenai(models))

    client.create(
        model="x",
        max_tokens=100,
        system="지침",
        tools=TOOL_SCHEMAS,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": "질문"}],
    )
    declared = models.seen[0]["config"].tools[0].function_declarations

    assert {d.name for d in declared} == {t["name"] for t in TOOL_SCHEMAS}


def test_the_anthropic_model_name_is_not_sent_to_gemini():
    models = FakeModels(Response([Candidate(Content([Part(text="답")]))]))
    client = GeminiMessages(FakeGenai(models), model="gemini-2.5-pro")

    client.create(
        model="claude-opus-5",
        max_tokens=100,
        system="지침",
        tools=TOOL_SCHEMAS,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": "질문"}],
    )

    assert models.seen[0]["model"] == "gemini-2.5-pro"


def test_a_thought_signature_survives_the_round_trip():
    """Gemini 3.x 는 도구 호출을 되돌려줄 때 받은 서명을 요구한다. 없으면 400.

    2026-09-09 실호출에서 실제로 겪었다. 공급자 중립 블록으로 정규화하면
    정확히 이 값이 버려진다.
    """
    call = Call(name="get_exception", args={"exception_id": "x"})
    part = Part(function_call=call)
    part.thought_signature = b"sig-abc"

    reply = _reply_of(Response([Candidate(Content([part]))]))
    assert reply.content[0].provider_state == b"sig-abc"

    converted = _contents([{"role": "assistant", "content": reply.content}])
    assert converted[0].parts[0].thought_signature == b"sig-abc"
