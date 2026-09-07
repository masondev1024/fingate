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

from .ledger import ExceptionLedger, ExceptionStatus


def _parse_now(value: str | None) -> dt.datetime:
    return dt.datetime.fromisoformat(value) if value else dt.datetime.now(dt.UTC)


def _print_row(staged) -> None:
    rules = ",".join(sorted({finding.rule for finding in staged.findings}))
    print(
        f"{staged.exception_id[:8]}  {staged.series_id:<28}{str(staged.status):<10}"
        f"{staged.expires_at.isoformat(timespec='minutes'):<26}{rules}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fingate-gate")
    parser.add_argument("--audit", type=Path, default=Path("data/audit.jsonl"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="예외 목록")
    listing.add_argument("--status", choices=[str(s) for s in ExceptionStatus], default=None)

    show = subparsers.add_parser("show", help="근거 상세")
    show.add_argument("exception_id")

    for name, help_text in (("approve", "승인"), ("reject", "반려")):
        decide = subparsers.add_parser(name, help=help_text)
        decide.add_argument("exception_id")
        decide.add_argument("--by", required=True, help="결정 주체")
        decide.add_argument("--note", required=True, help="사유 (필수)")
        decide.add_argument("--now", default=None, help="ISO8601, 테스트용")

    args = parser.parse_args(argv)
    ledger = ExceptionLedger(args.audit)

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

        decide = ledger.approve if args.command == "approve" else ledger.reject
        result = decide(
            args.exception_id, decided_by=args.by, note=args.note, now=_parse_now(args.now)
        )
        print(f"{result.status}: {result.exception_id} by {result.decided_by}")
        return 0
    except (KeyError, ValueError) as error:
        print(str(error).strip("'"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
