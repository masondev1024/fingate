"""승인 게이트 CLI.

담당자가 차단된 건을 보고 결정하는 인터페이스다. 이것이 없으면
"AI가 판단하고 사람이 승인한다"는 원칙에 실체가 없다.

대장 상태는 감사 로그에서 복원하므로 파이프라인과 별도 프로세스에서
실행해도 대기 중인 예외가 보인다.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

from .ledger import ExceptionLedger, ExceptionStatus, concurrence


def _parse_now(value: str | None) -> dt.datetime:
    return dt.datetime.fromisoformat(value) if value else dt.datetime.now(dt.UTC)


def _print_row(staged) -> None:
    rules = ",".join(sorted({finding.rule for finding in staged.findings}))
    print(
        f"{staged.exception_id[:8]}  {staged.series_id:<28}{str(staged.status):<10}"
        f"{staged.expires_at.isoformat(timespec='minutes'):<26}{rules}"
    )


def _recommendation_at_decision_time(args, exception_id: str, now: dt.datetime) -> str:
    """결정 시점에 에이전트가 무엇을 권고했는지 시스템이 직접 계산해 남긴다.

    승인자에게 "무엇을 봤느냐"고 물어 적게 하면 자기 신고가 되고, 그러면
    거수기 탐지가 무의미해진다. 나중에 사람이 에이전트와 다른 판단을 한 적이
    있는지 감사하려면 기록이 자기 신고여서는 안 된다.

    근거를 모으지 못하는 상황에서도 승인 자체는 막지 않는다. 게이트를 여는
    유일한 경로를 부수적인 실패로 잠그면 안 된다. 권고 없음으로 기록한다.

    창고가 없으면 열지 않고 바로 포기한다. DuckDB는 없는 경로를 조용히 새로
    만들기 때문에, 경로에 오타가 나면 빈 DB가 생기고 그 빈 DB에서 나온
    "근거 없음"이 진짜 판정처럼 기록된다. 모으지 못한 것과 모아서 없는 것은
    감사에서 전혀 다른 의미다.
    """
    # 지연 import: gate 계층이 review 계층에 구조적으로 의존하지 않는다.
    from ..collect.raw_store import RawStore
    from ..review.assemble import review_exception
    from ..serve.snapshot import ServingStore
    from ..warehouse.store import Warehouse

    if str(args.warehouse) != ":memory:" and not Path(args.warehouse).exists():
        return ""

    try:
        with Warehouse(args.warehouse) as warehouse:
            review = review_exception(
                exception_id,
                ledger=ExceptionLedger(args.audit),
                warehouse=warehouse,
                raw_store=RawStore(args.raw),
                serving=ServingStore(args.serving),
                now=now,
            )
        return str(review.recommendation.verdict)
    except Exception:  # noqa: BLE001 - 근거 수집 실패가 승인을 막아서는 안 된다
        return ""


def main(argv: list[str] | None = None) -> int:
    from ..review.cli import add_source_arguments

    parser = argparse.ArgumentParser(prog="fingate-gate")
    parser.add_argument("--audit", type=Path, default=Path("data/audit.jsonl"))
    add_source_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="예외 목록")
    listing.add_argument("--status", choices=[str(s) for s in ExceptionStatus], default=None)

    show = subparsers.add_parser("show", help="근거 상세")
    show.add_argument("exception_id")

    subparsers.add_parser("audit", help="사람의 결정이 에이전트 권고와 얼마나 일치했나")

    revoke = subparsers.add_parser("revoke", help="이미 내린 결정을 철회")
    revoke.add_argument("exception_id")
    revoke.add_argument("--by", required=True, help="철회 주체")
    revoke.add_argument("--note", required=True, help="사유 (필수)")
    revoke.add_argument("--now", default=None, help="ISO8601, 테스트용")

    for name, help_text in (("approve", "승인"), ("reject", "반려")):
        decide = subparsers.add_parser(name, help=help_text)
        decide.add_argument("exception_id")
        decide.add_argument("--by", required=True, help="결정 주체")
        decide.add_argument("--note", required=True, help="사유 (필수)")
        decide.add_argument("--now", default=None, help="ISO8601, 테스트용")

    args = parser.parse_args(argv)
    ledger = ExceptionLedger(args.audit)

    if args.command == "audit":
        report = concurrence(ledger)
        if report.rate is None:
            print("권고와 함께 결정된 건이 없다. 일치도를 계산할 수 없다.")
            withdrawn = ledger.list(status=ExceptionStatus.REVOKED)
            if withdrawn:
                print(f"철회된 결정           : {len(withdrawn)}")
            return 0
        print(f"권고와 함께 결정된 건 : {report.decided_with_recommendation}")
        print(f"권고와 일치           : {report.agreed}")
        print(f"권고와 불일치         : {report.disagreed}")
        print(f"일치율                : {report.rate:.3f}")
        withdrawn = ledger.list(status=ExceptionStatus.REVOKED)
        if withdrawn:
            print(f"철회된 결정           : {len(withdrawn)}")
        if report.rubber_stamp_risk:
            print()
            print(
                "주의: 표본 전체에서 권고와 100% 일치했다. 사람이 근거를 보지 않고\n"
                "권고를 그대로 승인하고 있을 수 있다. 승인 절차가 형식으로 굳는 신호다."
            )
        return 0

    if args.command == "list":
        status = ExceptionStatus(args.status) if args.status else ExceptionStatus.PENDING
        entries = ledger.list(status=status)
        if not entries:
            print(f"{status} 상태인 예외가 없습니다.")
            return 0
        print(f"{'id':<10}{'series':<28}{'status':<10}{'expires':<26}rules")
        for staged in sorted(entries, key=lambda item: item.created_at):
            _print_row(staged)
        return 0

    try:
        if args.command == "show":
            staged = ledger.get(args.exception_id)
            print(f"exception_id : {staged.exception_id}")
            print(f"series       : {staged.series_id}")
            print(f"status       : {staged.status}")
            print(f"created_at   : {staged.created_at.isoformat()}")
            print(f"expires_at   : {staged.expires_at.isoformat()}")
            print(f"request_id   : {staged.request_id}")
            if staged.decided_by:
                print(f"decided_by   : {staged.decided_by}")
                print(f"note         : {staged.decision_note}")
            print("findings:")
            for finding in staged.findings:
                period = finding.period.isoformat() if finding.period else "-"
                print(f"  [{finding.rule}] {period}  {finding.detail}")
            return 0

        now = _parse_now(args.now)
        if args.command == "revoke":
            result = ledger.revoke(
                args.exception_id, decided_by=args.by, note=args.note, now=_parse_now(args.now)
            )
            print(f"{result.status}: {result.exception_id} by {result.revoked_by}")
            print(f"  원래 결정은 기록에 남는다: {result.decided_by} — {result.decision_note}")
            print("  이미 승격된 last-known-good 스냅샷은 되돌리지 않는다.")
            return 0

        decide = ledger.approve if args.command == "approve" else ledger.reject
        result = decide(
            args.exception_id,
            decided_by=args.by,
            note=args.note,
            now=now,
            recommendation=_recommendation_at_decision_time(args, args.exception_id, now),
        )
        print(f"{result.status}: {result.exception_id} by {result.decided_by}")
        return 0
    except (KeyError, ValueError) as error:
        print(str(error).strip("'"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
