"""공급자 중립 응답 블록.

에이전트 루프는 Anthropic Messages API 모양(`.type` 이 붙은 콘텐츠 블록,
`stop_reason`)으로 짜여 있다. 다른 공급자를 붙일 때 루프를 고치는 대신
**어댑터가 이 모양으로 변환**한다.

이 파일이 있는 이유는 그 계약을 한곳에 적어 두기 위해서다. 루프는 아래 세
속성만 본다.

- 응답: `.content`(블록 목록), `.stop_reason`
- 텍스트 블록: `.type == "text"`, `.text`
- 도구 호출 블록: `.type == "tool_use"`, `.name`, `.input`, `.id`

Anthropic SDK 객체는 이미 이 모양이라 그대로 통과한다. 다른 공급자는 여기로
맞춘다. **근거 고정은 어느 쪽이든 동일하게 적용된다** — 그것이 이 분리의 요점이다.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    name: str
    input: dict
    id: str
    type: str = "tool_use"


@dataclass(frozen=True)
class ModelReply:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    stop_details: object = None
