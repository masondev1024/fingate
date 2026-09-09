"""승인자 질의 CLI — 차단된 건에 대해 자연어로 묻는다.

`fingate-review` 는 근거 전부를 정해진 형식으로 보여준다. 이 명령은 승인자가
가진 질문에 그 근거로 답한다. 답을 만드는 것은 모델이지만 **근거는 여전히
결정론적 probe 가 만든다.**

검증을 통과하지 못한 답변은 본문을 출력하지 않는다. 어떤 수치에 근거가 없는지
만 보여준다. 게이트가 데이터에 하는 일을 답변에도 그대로 한다.
"""

import argparse
import os
import sys
from pathlib import Path

from ..collect.raw_store import RawStore
from ..gate.ledger import ExceptionLedger
from ..review.cli import DEFAULT_AUDIT, add_source_arguments
from ..serve.snapshot import ServingStore
from ..warehouse.store import Warehouse
from .session import ask
from .tools import Toolbox

# 공급자마다 자격 증명 이름이 다르다. 하나만 있으면 된다.
PROVIDERS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _parse_now(value: str | None):
    import datetime as dt

    return dt.datetime.fromisoformat(value) if value else dt.datetime.now(dt.UTC)


def _credential(name: str, env_path: Path) -> str:
    """환경변수를 우선하고 없으면 .env 를 읽는다. 값은 절대 출력하지 않는다."""
    if os.environ.get(name):
        return os.environ[name]
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return ""


def _resolve_provider(requested: str | None, env_path: Path) -> tuple[str, str]:
    """어느 공급자로 돌릴지 정한다. 지정이 없으면 있는 키를 쓴다."""
    if requested:
        key = _credential(PROVIDERS[requested], env_path)
        return requested, key
    for provider, variable in PROVIDERS.items():
        key = _credential(variable, env_path)
        if key:
            return provider, key
    return "", ""


def _client_for(provider: str, key: str, model: str | None):
    if provider == "gemini":
        from .gemini import DEFAULT_MODEL, build_client

        return build_client(key, model=model or DEFAULT_MODEL)
    from .session import build_client

    return build_client(key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fingate-ask",
        description="차단된 건에 대해 근거로 답한다. 승인하지 않는다.",
    )
    parser.add_argument("exception_id")
    parser.add_argument("question", help="승인자의 질문")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    add_source_arguments(parser)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default=None)
    parser.add_argument("--model", default=None, help="공급자의 모델명")
    parser.add_argument("--now", default=None, help="ISO8601, 테스트용")
    args = parser.parse_args(argv)

    provider, key = _resolve_provider(args.provider, args.env)
    if not key:
        wanted = PROVIDERS[args.provider] if args.provider else " 또는 ".join(PROVIDERS.values())
        print(
            f"{wanted}가 없다. 이 명령만 모델 자격 증명을 필요로 한다.\n"
            "근거 조립과 검증은 fingate-review 로 키 없이 확인할 수 있다.",
            file=sys.stderr,
        )
        return 1

    if str(args.warehouse) != ":memory:" and not Path(args.warehouse).exists():
        print(f"창고가 없다: {args.warehouse}  (uv run fingate-run 을 먼저 실행)", file=sys.stderr)
        return 1

    with Warehouse(args.warehouse) as warehouse:
        toolbox = Toolbox(
            ledger=ExceptionLedger(args.audit),
            warehouse=warehouse,
            raw_store=RawStore(args.raw),
            serving=ServingStore(args.serving),
            now=_parse_now(args.now),
        )
        try:
            answer = ask(
                args.question,
                exception_id=args.exception_id,
                toolbox=toolbox,
                client=_client_for(provider, key, args.model),
            )
        except KeyError as error:
            print(str(error).strip("'"), file=sys.stderr)
            return 1

    print(f"공급자: {provider}")
    print(f"질문: {args.question}\n")
    if answer.tool_calls:
        print("읽은 근거")
        for name, arguments in answer.tool_calls:
            detail = arguments.get("probe") or arguments.get("status") or ""
            print(f"  {name}{f'  {detail}' if detail else ''}")
        print()

    if answer.refused:
        print(f"답변 없음: {answer.refusal_reason}", file=sys.stderr)
        return 1

    if not answer.released:
        print("근거를 확인할 수 없는 수치가 있어 답변을 내보내지 않는다.", file=sys.stderr)
        for token in answer.grounding.ungrounded:
            print(f"  {token} — 어떤 도구도 이 값을 반환하지 않았다", file=sys.stderr)
        print(
            "\n도구가 준 값만으로 답할 수 있는 질문으로 좁히거나, "
            "fingate-review 로 근거 전체를 직접 확인하라.",
            file=sys.stderr,
        )
        return 1

    print(answer.text)
    print()
    print(f"수치 {answer.grounding.checked}개 전부 근거 확인됨.")
    if answer.grounding.from_prompt:
        print(
            f"  이 중 {', '.join(answer.grounding.from_prompt)}는 질문에서 온 값이다. "
            "도구가 검증한 값이 아니다."
        )
    print("\n이 명령은 승인하지 않는다. 결정은 fingate-gate approve/reject 로만 기록된다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
