"""에이전트 루프 — 도구를 호출해 근거를 모으고, 검증을 통과한 답변만 내보낸다.

## 왜 수동 루프인가

SDK의 tool runner 를 쓰면 루프를 안 짜도 된다. 그런데 여기서는 두 가지가 필요하다.

1. **모든 도구 반환값을 모아야 한다.** 답변의 수치가 그 안에서 왔는지 검증하는
   것이 이 계층의 존재 이유다.
2. **네트워크 없이 테스트해야 한다.** 수집기가 전송 계층을 주입받는 것과 같은
   규율이다. CI 는 자격 증명 없이 돌아간다.

그래서 클라이언트를 프로토콜로 주입받고 루프를 직접 돈다.

## 왜 서버측 fallback 을 켜지 않았나

모델이 요청을 거절하면(`stop_reason == "refusal"`) 다른 모델로 자동 우회시킬 수
있다. 여기서는 켜지 않았다. **승인 근거를 조립하는 도중에 모델이 조용히 바뀌면,
그 답변이 어느 모델에서 나왔는지 감사할 수 없다.** 이 프로젝트가 반대하는 바로
그 형태의 조용한 대체다. 거절은 우회하지 않고 그대로 승인자에게 알린다.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from .grounding import GroundingReport, check_grounding
from .tools import TOOL_SCHEMAS, Toolbox, render

MODEL = "claude-opus-5"
MAX_TOKENS = 16000

# 도구 호출이 이보다 길어지면 답을 못 찾고 있는 것이다. 무한 루프를 막는다.
MAX_TURNS = 12

SYSTEM_PROMPT = """\
너는 금융 데이터 파이프라인의 승인 보조 도구다. 차단된 건에 대해 승인자가
던지는 질문에, 도구로 읽은 근거만으로 답한다.

지켜야 할 것:

1. **도구가 반환하지 않은 수치를 말하지 마라.** 금리, 표준편차, 시그마, 행 수,
   금액, 날짜 — 전부 도구 반환값에 있는 값만 쓴다. 기억이나 상식에서 끌어온
   숫자는 금지다. 어림값도 안 된다. 반올림해서 표기하는 것은 괜찮다.
2. **모르면 모른다고 하라.** 근거가 부족하면 무엇이 부족한지 말한다. 그럴듯한
   추측으로 빈칸을 메우지 않는다.
3. **너는 승인하지 않는다.** 결정은 사람이 사유를 적어야 기록된다. 권고를
   결정처럼 쓰지 마라.
4. 판정만 옮기지 말고 **그 판정의 근거가 된 측정값**을 함께 제시하라.
   승인자가 스스로 가중치를 정할 수 있어야 한다.

답변은 한국어로, 승인자가 결정할 수 있을 만큼 구체적으로 쓴다.\
"""


class MessagesClient(Protocol):
    """SDK 의 `client.messages` 에 해당하는 최소 면. 테스트가 가짜를 넣는다."""

    def create(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Answer:
    """에이전트의 답. 검증을 통과하지 못하면 본문을 내보내지 않는다."""

    text: str
    grounding: GroundingReport
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str = ""

    @property
    def released(self) -> bool:
        """승인자에게 보여도 되는가."""
        return not self.refused and self.grounding.ok


def _text_of(content: Any) -> str:
    return "".join(block.text for block in content if getattr(block, "type", "") == "text")


def ask(
    question: str,
    *,
    exception_id: str,
    toolbox: Toolbox,
    client: MessagesClient,
    model: str = MODEL,
    max_turns: int = MAX_TURNS,
) -> Answer:
    """승인자의 질문에 근거로 답한다.

    도구 반환값을 전부 모아 두었다가, 마지막 답변의 모든 수치가 그 안에서
    왔는지 확인한다. 하나라도 근거가 없으면 답변을 내보내지 않는다.
    """
    prompt = f"차단된 건 {exception_id}에 대한 질문이다.\n\n{question}"
    messages: list[dict] = [{"role": "user", "content": prompt}]
    collected: list[Any] = []
    calls: list[tuple[str, dict]] = []

    for _ in range(max_turns):
        response = client.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            thinking={"type": "adaptive"},
            messages=messages,
        )

        # 거절은 다른 모델로 우회시키지 않고 그대로 알린다.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            return Answer(
                text="",
                grounding=GroundingReport(checked=0),
                tool_calls=calls,
                refused=True,
                refusal_reason=getattr(details, "category", "") or "refusal",
            )

        tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if not tool_uses:
            answer = _text_of(response.content)
            return Answer(
                text=answer,
                grounding=check_grounding(answer, tool_results=collected, prompt_text=prompt),
                tool_calls=calls,
            )

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in tool_uses:
            arguments = dict(block.input)
            outcome = toolbox.run(block.name, arguments)
            collected.append(outcome)
            calls.append((block.name, arguments))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": render(outcome),
                }
            )
        # 병렬 호출 결과는 반드시 한 user 메시지에 모아 보낸다.
        messages.append({"role": "user", "content": results})

    return Answer(
        text="",
        grounding=GroundingReport(checked=0),
        tool_calls=calls,
        refused=True,
        refusal_reason=f"도구 호출이 {max_turns}회를 넘겨 중단했다",
    )


def build_client(api_key: str | None = None) -> MessagesClient:
    """실제 SDK 클라이언트. 자격 증명이 없으면 여기서 실패한다.

    지연 import 다 — 테스트와 CI 는 이 함수를 부르지 않고, anthropic 패키지가
    없어도 나머지 계층이 전부 돌아야 한다.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    return client.messages
