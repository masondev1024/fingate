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

**식별자도 통째로 다룬다.** 모델은 차단 건 id 를 그대로 인용한다. 숫자 추출기가
`c4e54b3d-550c-4a9b-...` 을 조각내면 하이픈을 음수 부호로 읽어 `-550` 까지 나온다.
실측에서 UUID 하나가 아홉 개의 "근거 없는 수치" 가 됐다. `dart:00113058`,
`722Y001`, `req-demo` 도 같은 문제를 만든다.

조각 단위로 인정하면 안 된다 — 지어낸 식별자의 조각도 대개 어딘가에 있기 때문이다.
**전체가 그대로 있어야 근거로 친다.**

**그리고 모델은 ISO 로 쓰지 않는다.** 실측에서 답변 6건이 막혔는데 원인이 전부
`2026년 9월 5일` 형태였다. 연도와 월은 우연히 통과하고 일자만 걸린다. 그래서
한국어 표기(`년 월 일`)와 점·슬래시 구분자도 날짜로 인식한다. 연·월만, 월·일만
쓴 형태도 있으므로 **있는 자리만 대조**한다.

## 실모델 산문에서만 드러난 것 (2026-09-09 실호출)

결정론적 요약문으로 잰 오탐률은 0이었다. 실제 모델 답변에 처음 돌리자 세 종류가
나왔고, **셋 다 검증기의 오탐이었다. 진짜 환각은 0건이었다.**

1. **버림.** 도구가 `0.014538` 을 주고 모델은 `0.014σ` 라고 썼다. 반올림이면
   `0.015` 다. 버림도 사람이 쓰는 정직한 표기이므로 마지막 자리 하나만 넓혀
   인정한다. `0.019` 는 여전히 잡힌다.
2. **필드 이름.** `p90` 의 `90`. `noise_p90` 은 도구가 반환한 **키 이름**이다.
   값이 아니라 이름을 인용한 것이므로 키에 들어 있는 숫자도 근거로 인정한다.
3. **목록 번호.** 모델은 근거를 `1. 2. 3.` 으로 쓴다. 실호출에서 1과 3은 우연히
   통과하고 2만 잡혔다 — 그 자체가 이것이 수치 주장이 아니라는 증거다.

세 번째가 특히 T-20과 같은 교훈이다. **요약문으로 재고 "오탐 0" 이라고 적은 것이
실제 문장 형태를 덮지 못했다.** 무엇을 재고 있는지가 항상 먼저다.
"""

import math
import re
from dataclasses import dataclass, field

# 소수 자릿수를 표시된 그대로 세야 하므로 유효숫자가 아니라 문자열로 다룬다.
_NUMBER = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 도구는 시각을 ISO 로 반환하고 모델은 그대로 옮긴다. 날짜만 떼면 뒤의
# 시·분·초와 마이크로초가 조각난다 — 실측에서 508517 이 그렇게 잡혔다.
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)

# 모델이 실제로 쓰는 날짜 표기들. 있는 자리만 대조하려고 각각 따로 잡는다.
# 넓은 것부터 좁은 것 순서여야 "2026년 9월 5일" 이 "2026년 9월" 로 먼저 먹히지 않는다.
_DATE_FORMS = (
    re.compile(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?"),
    re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월"),
    re.compile(r"(?<![\d년])(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
)

# 비율을 백분율로 읽은 경우만 100배를 허용한다. 숫자 바로 뒤의 % 기호를 본다.
_PERCENT_SUFFIX = re.compile(r"\s*%")

# 식별자로 볼 토큰. 글자와 숫자가 섞인 코드·id 다. 날짜는 글자가 없어 걸리지 않는다.
# 구분자(- :)는 영숫자 **사이에만** 허용한다. 끝에 붙이면 "p90:" 처럼
# 문장부호까지 삼켜 도구 반환값과 대조되지 않는다.
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+(?:[-:][A-Za-z0-9_]+)*")

# 마크다운 순서 목록 표식. 줄머리의 "2." 나 "**2.**" 는 측정값이 아니다.
_LIST_MARKER = re.compile(r"^[ \t]*(?:[*_]{0,2})\d+[.)](?:[*_]{0,2})(?=\s)", re.MULTILINE)


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
    """중첩 구조를 평탄화한다. probe 의 facts 는 리스트 안의 딕셔너리다.

    **키 이름도 함께 낸다.** 모델은 `noise_p90` 을 "p90" 이라고 인용한다.
    값이 아니라 도구가 준 이름을 옮긴 것이므로 근거로 인정해야 한다.
    """
    if isinstance(value, dict):
        return [*value.keys()] + [item for child in value.values() for item in _walk(child)]
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


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _stamp_digits(text: str) -> list[str]:
    """도구가 준 시각들을 숫자만 남긴 형태로. 모델이 다시 포맷해도 접두로 맞는다.

    2026-09-08T00:55:13.508517+00:00 -> "202609080055135085170000"
    모델이 "2026-09-08 00:55:13" 으로 줄여 써도 그 접두라서 대조된다.
    """
    return [_digits(m.group()) for m in _TIMESTAMP.finditer(text)]


def _identifier_like(token: str) -> bool:
    """코드나 id 로 볼 만한가. 숫자와 글자가 섞여 있고 순수 수치가 아니어야 한다."""
    if not any(ch.isdigit() for ch in token):
        return False
    return any(ch.isalpha() for ch in token)


def _text_values(source: object) -> set[str]:
    """도구 반환값에 문자열로 등장한 모든 것. 식별자를 통째로 대조하는 데 쓴다."""
    return {str(leaf) for leaf in _walk(source) if isinstance(leaf, str)}


def _date_parts(dates: set[str]) -> list[tuple[int, int, int]]:
    parts = []
    for value in dates:
        year, month, day = value.split("-")
        parts.append((int(year), int(month), int(day)))
    return parts


def _known_date(fields: tuple[int | None, int | None, int | None], known) -> bool:
    """있는 자리만 대조한다. 연도를 안 쓴 표기도 모델은 실제로 쓴다."""
    year, month, day = fields
    for candidate in known:
        if year is not None and year != candidate[0]:
            continue
        if month is not None and month != candidate[1]:
            continue
        if day is not None and day != candidate[2]:
            continue
        return True
    return False


def _fields_of(match: re.Match) -> tuple[int | None, int | None, int | None]:
    groups = [int(g) for g in match.groups()]
    if len(groups) == 3:
        return groups[0], groups[1], groups[2]
    # 두 자리만 있는 형태. 첫 값이 4자리면 연·월, 아니면 월·일이다.
    if groups[0] > 31:
        return groups[0], groups[1], None
    return None, groups[0], groups[1]


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
    places = _decimals(stated)
    stated_value = round(value, places)
    if round(scaled, places) == stated_value:
        return True
    # 버림도 사람이 쓰는 표기다. 마지막 자리 하나만 넓힌다 — 그 이상 열면
    # 어긋난 값이 통과하기 시작한다.
    step = 10.0**-places
    truncated = math.floor(abs(scaled) / step) * step * (1 if scaled >= 0 else -1)
    return round(truncated, places) == stated_value


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

    # 목록 번호를 먼저 지운다. 측정값이 아니라 서식이다.
    remaining = _LIST_MARKER.sub(" ", answer)

    tool_text = " ".join(_text_values(tool_results))
    prompt_text_all = str(prompt_text)

    # 시각을 가장 먼저 통째로 처리한다. 날짜 규칙보다 앞이어야 뒤가 안 남는다.
    tool_stamps = _stamp_digits(tool_text)
    prompt_stamps = _stamp_digits(prompt_text_all)
    for match in list(_TIMESTAMP.finditer(remaining)):
        token = match.group()
        stated = _digits(token)
        checked += 1
        if any(known.startswith(stated) for known in tool_stamps):
            sources[token] = "tool"
        elif any(known.startswith(stated) for known in prompt_stamps):
            sources.setdefault(token, "prompt")
        elif token not in ungrounded:
            ungrounded.append(token)
        remaining = remaining.replace(token, " ", 1)

    # 식별자를 통째로 처리한다. 숫자보다 앞이어야 조각나지 않는다.
    for match in list(_IDENTIFIER.finditer(remaining)):
        token = match.group()
        if not _identifier_like(token):
            continue
        checked += 1
        if token in tool_text:
            sources[token] = "tool"
        elif token in prompt_text_all:
            sources.setdefault(token, "prompt")
        elif token not in ungrounded:
            ungrounded.append(token)
        remaining = remaining.replace(token, " ", 1)

    # 날짜를 처리하고 본문에서 지운다. 남은 자리에서 숫자를 찾는다.
    tool_parts = _date_parts(tool_dates)
    prompt_parts = _date_parts(prompt_dates)
    for pattern in _DATE_FORMS:
        for match in list(pattern.finditer(remaining)):
            token = match.group().strip()
            fields = _fields_of(match)
            checked += 1
            if _known_date(fields, tool_parts):
                sources[token] = "tool"
            elif _known_date(fields, prompt_parts):
                sources.setdefault(token, "prompt")
            elif token not in ungrounded:
                ungrounded.append(token)
            remaining = remaining.replace(match.group(), " ", 1)

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
