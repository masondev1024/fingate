"""Gemini 어댑터 — 같은 루프를 다른 공급자로 돌린다.

에이전트 루프는 클라이언트를 프로토콜로 주입받는다(`session.MessagesClient`).
그래서 공급자를 바꾸는 일은 루프를 고치는 것이 아니라 **어댑터를 하나 더 쓰는
것**이다. 수집기가 전송 계층을 주입받아 ECOS·DART를 같은 계약으로 다루는 것과
같은 구조다.

**근거 고정은 공급자와 무관하게 적용된다.** 모델이 무엇이든, 도구가 반환하지
않은 수치를 말하면 답변은 나가지 않는다. 이 분리가 없으면 "환각을 막았다"는
주장이 특정 모델의 성질에 기대게 된다.

## 두 API의 차이 중 실제로 문제가 되는 것

- Gemini는 도구 호출을 **자동으로 실행**하려 한다. 껐다. 자동 실행하면 도구
  반환값이 SDK 안에서 소비되어 근거 검증에 쓸 수 없다.
- 도구 결과를 되돌려줄 때 Gemini는 호출 **id 가 아니라 함수 이름**을 요구한다.
  그래서 어댑터가 대화를 변환하면서 id -> 이름 대응을 만들어 둔다.
- Gemini의 역할 이름은 `model` 이고 Anthropic은 `assistant` 다.
- Gemini 3.x 는 도구 호출을 이력에 되돌려줄 때 **함께 온 `thought_signature` 를
  그대로 다시 붙일 것**을 요구한다. 없으면 400 이다. 응답을 공급자 중립 블록으로
  정규화하면 정확히 이 값이 버려지므로, 블록이 불투명 상태로 실어 나른다.
"""

import json
from typing import Any

from .blocks import ModelReply, TextBlock, ToolUseBlock

# 2026-09-09 실측: gemini-2.5-pro 는 신규 사용자에게 404 를 준다. API 가
# 후속 모델로 gemini-3.1-pro-preview 를 지목했다. 모델명은 계정별로 다를 수
# 있으므로 --model 로 덮어쓸 수 있다.
DEFAULT_MODEL = "gemini-3.1-pro-preview"


def _tool_declarations(tools: list[dict]):
    """Anthropic 도구 스키마를 Gemini 함수 선언으로 옮긴다.

    `input_schema` 를 `parameters_json_schema` 로 그대로 넘긴다. `strict` 는
    Anthropic 쪽 개념이라 버린다 — Gemini 에는 대응이 없다.
    """
    from google.genai import types

    return [
        types.FunctionDeclaration(
            name=tool["name"],
            description=tool["description"],
            parameters_json_schema=tool["input_schema"],
        )
        for tool in tools
    ]


def _parts_of(content: Any, names: dict[str, str]):
    """Anthropic 모양 메시지 하나를 Gemini part 목록으로 옮긴다."""
    from google.genai import types

    if isinstance(content, str):
        return [types.Part.from_text(text=content)]

    parts = []
    for block in content:
        if isinstance(block, dict):
            # 루프가 만든 tool_result 블록. 이름은 앞선 도구 호출에서 찾는다.
            if block.get("type") == "tool_result":
                name = names.get(block["tool_use_id"], "unknown_tool")
                payload = block["content"]
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    payload = {"result": payload}
                parts.append(types.Part.from_function_response(name=name, response=payload))
            elif block.get("type") == "text":
                parts.append(types.Part.from_text(text=block["text"]))
            continue

        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(types.Part.from_text(text=block.text))
        elif kind == "tool_use":
            names[block.id] = block.name
            call = types.FunctionCall(name=block.name, args=dict(block.input))
            # 받은 서명을 그대로 되돌려준다. 새로 만들 수 있는 값이 아니다.
            parts.append(
                types.Part(
                    function_call=call,
                    thought_signature=getattr(block, "provider_state", None),
                )
            )
    return parts


def _contents(messages: list[dict]):
    """대화 전체를 옮긴다. 도구 호출 id -> 이름 대응을 만들면서 진행한다."""
    from google.genai import types

    names: dict[str, str] = {}
    converted = []
    for message in messages:
        parts = _parts_of(message["content"], names)
        if not parts:
            continue
        role = "model" if message["role"] == "assistant" else "user"
        converted.append(types.Content(role=role, parts=parts))
    return converted


class GeminiMessages:
    """`session.MessagesClient` 를 만족하는 Gemini 어댑터."""

    def __init__(self, client: Any, model: str = DEFAULT_MODEL) -> None:
        self._client = client
        self._model = model

    def create(self, **kwargs: Any) -> ModelReply:
        from google.genai import types

        response = self._client.models.generate_content(
            # 호출자가 넘긴 Anthropic 모델명은 무시한다. 공급자가 다르다.
            model=self._model,
            contents=_contents(kwargs["messages"]),
            config=types.GenerateContentConfig(
                system_instruction=kwargs.get("system"),
                tools=[types.Tool(function_declarations=_tool_declarations(kwargs["tools"]))],
                max_output_tokens=kwargs.get("max_tokens"),
                # 자동 실행을 켜두면 도구 반환값이 SDK 안에서 소비되어
                # 근거 검증에 쓸 수 없다. 루프는 우리가 돈다.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        return _reply_of(response)


def _reply_of(response: Any) -> ModelReply:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ModelReply(content=[], stop_reason="refusal")

    candidate = candidates[0]
    parts = getattr(getattr(candidate, "content", None), "parts", None) or []

    blocks: list = []
    for index, part in enumerate(parts):
        call = getattr(part, "function_call", None)
        if call is not None:
            blocks.append(
                ToolUseBlock(
                    name=call.name,
                    input=dict(call.args or {}),
                    id=getattr(call, "id", None) or f"{call.name}-{index}",
                    provider_state=getattr(part, "thought_signature", None),
                )
            )
            continue
        text = getattr(part, "text", None)
        # 사고 과정 part 는 답변 본문이 아니다. 근거 검증 대상에서 제외한다.
        if text and not getattr(part, "thought", False):
            blocks.append(TextBlock(text=text))

    if any(block.type == "tool_use" for block in blocks):
        return ModelReply(content=blocks, stop_reason="tool_use")

    # 안전 필터로 잘린 응답을 빈 답변으로 넘기지 않는다. 거절로 다룬다.
    finish = str(getattr(candidate, "finish_reason", "") or "")
    if not blocks or "SAFETY" in finish.upper() or "BLOCK" in finish.upper():
        return ModelReply(content=blocks, stop_reason="refusal")
    return ModelReply(content=blocks, stop_reason="end_turn")


def build_client(api_key: str, model: str = DEFAULT_MODEL) -> GeminiMessages:
    """Gemini 클라이언트. 자격 증명이 없으면 여기서 실패한다."""
    from google import genai

    return GeminiMessages(genai.Client(api_key=api_key), model=model)
