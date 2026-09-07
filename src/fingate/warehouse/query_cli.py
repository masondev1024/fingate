"""서빙·분석 조회 CLI.

파이썬 API만 있으면 "서빙"이라는 말에 실체가 없다. 사람이 값을 꺼내 볼
수단이 있어야 한다.

임의 SQL은 받지 않는다. 이름 붙은 조회만 노출해 무엇을 볼 수 있는지가
곧 계약이 되게 한다.
"""

import argparse
import datetime as dt
import math
import sys
import unicodedata
from pathlib import Path

from .store import Warehouse

# 표본이 이보다 적으면 상관계수를 신호로 읽지 않는다.
MIN_SAMPLE_FOR_CORRELATION = 30
TRILLION = 1e12
HUNDRED_MILLION = 1e8


def _display_width(text: str) -> int:
    """한글과 전각 문자는 터미널에서 두 칸을 차지한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return " " * max(0, width - _display_width(text)) + text


def _cell(value: object, spec: str) -> str:
    """값을 먼저 포맷하고 정렬은 나중에 한다.

    포맷 스펙과 정렬을 한 문자열로 조립하면 부호와 폭의 순서가 뒤바뀐다.
    ">8+.3f" 는 유효하지 않고 ">+8.3f" 여야 한다.
    """
    if value is None:
        return "-"
    if isinstance(value, float) and math.isnan(value):
        return "-"
    if spec == "s":
        return str(value)
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return str(value)


def _table(rows: list[dict], columns: list[tuple[str, str, str]]) -> None:
    """columns는 (키, 표시명, 값 포맷) 목록이다. 폭은 내용에 맞춘다."""
    if not rows:
        print("결과가 없습니다.")
        return
    body = [[_cell(row.get(key), spec) for key, _, spec in columns] for row in rows]
    widths = [
        max(
            _display_width(label),
            max((_display_width(row[index]) for row in body), default=0),
        )
        + 2
        for index, (_, label, _) in enumerate(columns)
    ]
    print("".join(_pad(label, w) for (_, label, _), w in zip(columns, widths, strict=True)))
    for row in body:
        print("".join(_pad(cell, w) for cell, w in zip(row, widths, strict=True)))


def _scaled(rows: list[dict], mapping: dict[str, float]) -> list[dict]:
    """큰 금액은 조·억 단위로 줄여야 사람이 읽는다."""
    scaled = []
    for row in rows:
        item = dict(row)
        for key, divisor in mapping.items():
            if item.get(key) is not None:
                item[key] = item[key] / divisor
        scaled.append(item)
    return scaled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fingate-query")
    parser.add_argument("--db", type=Path, default=Path("data/fingate.duckdb"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serving", help="서빙 테이블")
    change = subparsers.add_parser("change", help="분기 대비 변화")
    change.add_argument("--corp", default=None)
    subparsers.add_parser("sensitivity", help="금리 민감도")
    freshness = subparsers.add_parser("freshness", help="시계열 신선도")
    freshness.add_argument("--as-of", default=None, help="YYYY-MM-DD")

    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"database not found: {args.db} (run fingate-run first)", file=sys.stderr)
        return 1

    warehouse = Warehouse(args.db)

    if args.command == "serving":
        rows = _scaled(
            warehouse.serving_rows(),
            {"total_assets": TRILLION, "net_income_quarter": HUNDRED_MILLION},
        )
        _table(
            rows,
            [
                ("corp_name", "회사", "s"),
                ("year", "연도", "d"),
                ("quarter", "분기", "d"),
                ("total_assets", "자산총계(조)", ",.1f"),
                ("net_income_quarter", "분기순이익(억)", ",.0f"),
                ("net_income_is_derived", "유도", "s"),
                ("base_rate", "기준금리", ".2f"),
            ],
        )
        return 0

    if args.command == "change":
        sql = "SELECT * FROM analytics_insurer_quarterly_change"
        parameters: tuple = ()
        if args.corp:
            sql += " WHERE corp_name = ?"
            parameters = (args.corp,)
        sql += " ORDER BY corp_name, year, quarter"
        rows = _scaled(warehouse.query(sql, parameters), {"total_assets": TRILLION})
        _table(
            rows,
            [
                ("corp_name", "회사", "s"),
                ("year", "연도", "d"),
                ("quarter", "분기", "d"),
                ("total_assets", "자산총계(조)", ",.1f"),
                ("assets_qoq_pct", "자산증감률", "+.2f"),
                ("base_rate", "기준금리", ".2f"),
                ("rate_qoq_delta", "금리변화", "+.2f"),
            ],
        )
        return 0

    if args.command == "sensitivity":
        rows = warehouse.query("SELECT * FROM analytics_rate_sensitivity ORDER BY corp_name")
        _table(
            rows,
            [
                ("corp_name", "회사", "s"),
                ("quarters_compared", "비교분기", "d"),
                ("correlation", "상관계수", "+.3f"),
                ("avg_assets_qoq_pct", "평균자산증감률", ".2f"),
            ],
        )
        thin = [r for r in rows if (r["quarters_compared"] or 0) < MIN_SAMPLE_FOR_CORRELATION]
        if thin:
            print(
                f"\n주의: 표본이 {MIN_SAMPLE_FOR_CORRELATION}분기 미만인 계열이 "
                f"{len(thin)}개다. IFRS17 시행(2023)으로 보험사 재무가 2023년부터만 "
                "존재해 시점이 부족하다. 위 상관계수는 신호가 아니라 잡음으로 읽어야 한다."
            )
        return 0

    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
    _table(
        warehouse.freshness(as_of=as_of),
        [
            ("series_id", "series_id", "s"),
            ("latest_period", "최신", "s"),
            ("observation_count", "관측수", ",d"),
            ("age_days", "경과일", "d"),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
