"""승인 보조 CLI — 차단된 건의 근거를 조립해 보여준다.

이 명령은 아무것도 결정하지 않는다. 승인은 `fingate-gate approve`로만,
주체와 사유를 적어야만 이뤄진다. 화면에도 그렇게 적는다. 권고가 결정으로
오독되는 순간 사람의 승인은 형식이 된다.
"""

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ..collect.raw_store import RawStore
from ..gate.ledger import ExceptionLedger
from ..serve.snapshot import ServingStore
from ..warehouse.store import Warehouse
from .assemble import review_exception

DEFAULT_AUDIT = Path("data/audit.jsonl")
DEFAULT_WAREHOUSE = Path("data/fingate.duckdb")
DEFAULT_RAW = Path("data/raw")
DEFAULT_SERVING = Path("data/serving")


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    """근거를 모으는 데 필요한 저장소 경로. gate CLI도 같은 것을 쓴다."""
    parser.add_argument("--warehouse", type=Path, default=DEFAULT_WAREHOUSE)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--serving", type=Path, default=DEFAULT_SERVING)


def _parse_now(value: str | None) -> dt.datetime:
    return dt.datetime.fromisoformat(value) if value else dt.datetime.now(dt.UTC)


def _render(review, exception_id: str) -> None:
    staged = review.exception
    print(f"exception_id : {exception_id}")
    print(f"series       : {staged.series_id}")
    print(f"status       : {staged.status}")
    print(f"expires_at   : {staged.expires_at.isoformat(timespec='minutes')}")
    print()
    print("차단 사유")
    for finding in staged.findings:
        period = finding.period.isoformat() if finding.period else "-"
        print(f"  [{finding.rule}] {period}  {finding.detail}")
    print()
    print("근거")
    for item in review.evidence:
        print(f"  {item.probe} — {item.verdict}")
        print(f"    {item.summary}")
        for entry in item.facts.get("peers", []) if isinstance(item.facts, dict) else []:
            change = entry["peer_change"]
            moved = "동행" if entry["co_moved"] else "미동행"
            shown = "관측 없음" if change is None else f"{change:+.3f}"
            print(
                f"      {entry['peer_id']}: {shown} ({moved}) "
                f"동행률 {entry['agreement']:.3f}, 초과 {entry['lift']:+.3f}, "
                f"평시 상위10% {entry['noise_p90']:.3f}"
            )
    print()
    print(f"권고: {review.recommendation.verdict}")
    print(f"  {review.recommendation.because}")
    print()
    print("이 명령은 승인하지 않는다. 결정은 사람이 사유를 적어야 기록된다:")
    print(f"  fingate-gate approve {exception_id} --by <이름> --note <사유>")
    print(f"  fingate-gate reject  {exception_id} --by <이름> --note <사유>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fingate-review", description="차단된 건의 근거를 조립한다. 승인하지 않는다."
    )
    parser.add_argument("exception_id")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    add_source_arguments(parser)
    parser.add_argument("--now", default=None, help="ISO8601, 테스트용")
    parser.add_argument("--json", action="store_true", help="기계 판독용 출력")
    args = parser.parse_args(argv)

    ledger = ExceptionLedger(args.audit)
    try:
        with Warehouse(args.warehouse) as warehouse:
            review = review_exception(
                args.exception_id,
                ledger=ledger,
                warehouse=warehouse,
                raw_store=RawStore(args.raw),
                serving=ServingStore(args.serving),
                now=_parse_now(args.now),
            )
    except KeyError as error:
        print(str(error).strip("'"), file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "exception_id": args.exception_id,
                    "series_id": review.exception.series_id,
                    "status": str(review.exception.status),
                    "evidence": [asdict(item) for item in review.evidence],
                    "recommendation": {
                        "verdict": str(review.recommendation.verdict),
                        "because": review.recommendation.because,
                    },
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        _render(review, args.exception_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
