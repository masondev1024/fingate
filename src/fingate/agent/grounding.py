"""근거 고정 — 모델이 말한 수치가 도구 반환값에서 왔는지 검증한다.

이 프로젝트는 계약을 통과하지 못한 값을 내보내지 않는다. LLM이라고 예외를 둘
이유가 없다. 모델이 도구가 주지 않은 숫자를 말하면 그 답변은 내보내지 않는다.

금융에서 LLM의 실패는 "말투가 어색하다"가 아니라 **없는 수치를 그럴듯하게
말한다**로 나타난다. 승인자가 그 수치를 근거로 게이트를 열면, 감사 로그에는
사유가 남지만 그 사유의 근거는 존재한 적이 없다. T-18에서 실제로 겪은 일의
LLM 버전이다.

## 무엇을 근거로 인정하는가

- `tool` — 도구가 반환한 값. **검증된 근거다.**
- `prompt` — 승인자가 질문에 쓴 값. 지어낸 것은 아니지만 검증된 것도 아니다.
  구분해서 표시한다.
- 그 외 — 근거 없음. 답변을 내보내지 않는다.

## 반올림을 어떻게 다루는가

도구가 15.083을 주고 모델이 "15.1σ"라고 쓰는 것은 환각이 아니라 읽기 좋은
표기다. 그래서 **표시된 자릿수로 반올림했을 때 일치하면** 근거로 인정한다.
15.083을 15.9로 쓰는 것은 어떤 자릿수로도 반올림되지 않으므로 잡힌다.

비율은 예외를 하나 둔다. 도구가 0.922를 주고 사람은 92.2%로 읽는다. 다만
**`%` 기호가 붙은 경우에만** 100배를 허용한다. 기호 없이 92.2라고 쓰면
인정하지 않는다 — 그 관용을 열어 두면 두 자리 어긋난 수치가 전부 통과한다.

날짜는 숫자 셋으로 쪼개지 않고 통째로 대조한다. 쪼개면 `2026-09-04`가
2026·09·04 세 개의 근거를 요구하게 되고, 그중 하나라도 없으면 정상 답변이
환각으로 몰린다.
"""

import re
from dataclasses import dataclass, field

# 소수 자릿수를 표시된 그대로 세야 하므로 유효숫자가 아니라 문자열로 다룬다.
_NUMBER = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 비율을 백분율로 읽은 경우만 100배를 허용한다. 숫자 바로 뒤의 % 기호를 본다.
_PERCENT_SUFFIX = re.compile(r"\s*%")


@dataclass(frozen=True)
class GroundingReport:
    """검증 결과. 판정만이 아니라 각 수치의 출처를 함께 남긴다."""

    checked: int
    sources: dict[str, str] = field(default_factory=dict)
    ungrounded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    @property
    def from_prompt(self) -> list[str]:
        """질문에서만 온 수치. 지어낸 것은 아니지만 검증된 근거도 아니다."""
        return sorted(token for token, source in self.sources.items() if source == "prompt")


def _walk(value: object) -> list[object]:
    """중첩 구조를 평탄화한다. probe 의 facts 는 리스트 안의 딕셔너리다."""
    if isinstance(value, dict):
        return [item for child in value.values() for item in _walk(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _walk(child)]
    return [value]


def _numeric_values(source: object) -> list[float]:
    numbers: list[float] = []
    for leaf in _walk(source):
        if isinstance(leaf, bool):
            continue
        if isinstance(leaf, (int, float)):
            if leaf == leaf:  # nan 은 무엇과도 일치하지 않는다
                numbers.append(float(leaf))
        elif isinstance(leaf, str):
            for token in _NUMBER.findall(leaf):
                parsed = _parse(token)
                if parsed is not None:
                    numbers.append(parsed)
    return numbers


def _date_values(source: object) -> set[str]:
    dates: set[str] = set()
    for leaf in _walk(source):
        if isinstance(leaf, str):
            dates.update(_ISO_DATE.findall(leaf))
    return dates


def _parse(token: str) -> float | None:
    cleaned = token.replace(",", "").replace("−", "-")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _decimals(token: str) -> int:
    _, _, fraction = token.partition(".")
    return len(fraction)


def _matches(stated: str, candidate: float, *, as_percent: bool) -> bool:
    value = _parse(stated)
    if value is None:
        return False
    scaled = candidate * 100 if as_percent else candidate
    # 표시된 자릿수로 반올림했을 때 같으면 같은 값을 읽기 좋게 쓴 것이다.
    places = _decimals(stated)
    return round(scaled, places) == round(value, places)


def check_grounding(answer: str, *, tool_results: list, prompt_text: str) -> GroundingReport:
    """답변의 모든 수치가 도구 반환값 또는 질문에서 왔는지 확인한다.

    하나라도 근거가 없으면 `ok` 가 False다. 호출자는 그 답변을 내보내지 않는다.
    """
    tool_numbers = _numeric_values(tool_results)
    prompt_numbers = _numeric_values(prompt_text)
    tool_dates = _date_values(tool_results)
    prompt_dates = _date_values(prompt_text)

    sources: dict[str, str] = {}
    ungrounded: list[str] = []
    checked = 0

    # 날짜를 먼저 처리하고 본문에서 지운다. 남은 자리에서 숫자를 찾는다.
    remaining = answer
    for date in _ISO_DATE.findall(answer):
        checked += 1
        if date in tool_dates:
            sources[date] = "tool"
        elif date in prompt_dates:
            sources[date] = "prompt"
        elif date not in ungrounded:
            ungrounded.append(date)
        remaining = remaining.replace(date, " ")

    for match in _NUMBER.finditer(remaining):
        token = match.group()
        checked += 1
        as_percent = bool(_PERCENT_SUFFIX.match(remaining[match.end() :]))

        if any(_matches(token, value, as_percent=False) for value in tool_numbers) or (
            as_percent and any(_matches(token, v, as_percent=True) for v in tool_numbers)
        ):
            sources[token] = "tool"
        elif any(_matches(token, value, as_percent=False) for value in prompt_numbers) or (
            as_percent and any(_matches(token, v, as_percent=True) for v in prompt_numbers)
        ):
            sources.setdefault(token, "prompt")
        elif token not in ungrounded:
            ungrounded.append(token)

    return GroundingReport(checked=checked, sources=sources, ungrounded=ungrounded)
