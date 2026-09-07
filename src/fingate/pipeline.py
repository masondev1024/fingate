"""파이프라인 실행 진입점.

수집 → 응답 계약 → 원본 보존 → 스키마 계약 → 품질 계약 → 승인 게이트 →
서빙 판정 → 적재를 한 번에 실행한다.

이 진입점이 없으면 저장소를 받은 사람이 README의 산출물을 재현할 수 없다.
"""

import argparse
import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

from .collect.dart import DartCollector, ReportCode
from .collect.ecos import ContractViolation, EcosCollector, EcosRequest
from .collect.raw_store import RawStore
from .collect.series import SERIES
from .contracts.quality import check_quality
from .contracts.schema import normalize_ecos_rows
from .gate.ledger import ExceptionLedger
from .serve.snapshot import ServingStore, decide_serving
from .warehouse.store import Warehouse

# 보험 5사. 수익 구조가 금리에 직결되어 두 소스의 조인이 의미를 갖는다.
INSURERS = {
    "00113058": "한화생명",
    "00135917": "한화손해보험",
    "00126256": "삼성생명",
    "00159102": "DB손해보험",
    "00164973": "현대해상",
}
REPORTS = (ReportCode.Q1, ReportCode.HALF, ReportCode.Q3, ReportCode.ANNUAL)
EXCEPTION_TTL = dt.timedelta(days=2)


@dataclass
class RunSummary:
    rate_rows: int = 0
    financial_rows: int = 0
    unmapped_rows: int = 0
    blocked_requests: int = 0
    staged_exceptions: int = 0
    serving_states: dict[str, str] = field(default_factory=dict)


def load_credentials(env_path: Path) -> dict[str, str]:
    """환경변수를 우선하고, 없으면 .env를 읽는다. 값은 로그에 남기지 않는다."""
    credentials = {
        name: os.environ[name] for name in ("ECOS_API_KEY", "DART_API_KEY") if os.environ.get(name)
    }
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            credentials.setdefault(name.strip(), value.strip())
    missing = [n for n in ("ECOS_API_KEY", "DART_API_KEY") if not credentials.get(n)]
    if missing:
        raise SystemExit(f"missing credentials: {missing} (see .env.example)")
    return credentials


def run(
    data_dir: Path,
    env_path: Path,
    rate_start: str,
    rate_end: str,
    years: tuple[int, ...],
    now: dt.datetime,
) -> RunSummary:
    credentials = load_credentials(env_path)
    summary = RunSummary()

    raw = RawStore(data_dir / "raw")
    warehouse = Warehouse(data_dir / "fingate.duckdb")
    serving = ServingStore(data_dir / "serving")
    ledger = ExceptionLedger(data_dir / "audit.jsonl")
    as_of = now.date()

    ecos = EcosCollector(credentials["ECOS_API_KEY"], raw)
    monthly_start, monthly_end = rate_start[:6], rate_end[:6]

    for spec in SERIES:
        start, end = (rate_start, rate_end) if spec.cycle == "D" else (monthly_start, monthly_end)
        try:
            collected = ecos.collect(
                EcosRequest(
                    spec.stat_code,
                    spec.cycle,
                    start,
                    end,
                    item_code=spec.item_code,
                    end_row=5000,
                )
            )
        except ContractViolation as violation:
            summary.blocked_requests += 1
            summary.serving_states[spec.series_id] = f"blocked:{violation.code}"
            continue

        normalized = normalize_ecos_rows(spec, collected.rows, request_id=collected.raw.request_id)
        report = check_quality(spec, normalized.observations, as_of=as_of)
        decision = decide_serving(spec, report, normalized.observations, serving, ledger, now)
        summary.serving_states[spec.series_id] = str(decision.state)

        # 위반은 자동으로 막되, 사람이 판단할 수 있도록 대장에 올린다.
        if report.findings and not ledger.is_cleared(spec.series_id, now):
            ledger.stage(
                spec.series_id, report.findings, collected.raw.request_id, now, EXCEPTION_TTL
            )
            summary.staged_exceptions += 1

        if decision.is_current:
            summary.rate_rows += warehouse.load_observations(
                decision.observations, loaded_at=now
            ).inserted

    dart = DartCollector(credentials["DART_API_KEY"], raw)
    for corp_code, corp_name in INSURERS.items():
        for year in years:
            for report_code in REPORTS:
                try:
                    financials = dart.collect_financials(corp_code, year, report_code)
                except ContractViolation:
                    summary.blocked_requests += 1
                    continue
                loaded = warehouse.load_financials(
                    corp_code=corp_code,
                    corp_name=corp_name,
                    bsns_year=year,
                    report_code=report_code.value,
                    rows=financials.rows,
                    request_id=financials.raw.request_id,
                    loaded_at=now,
                )
                summary.financial_rows += loaded.inserted
                summary.unmapped_rows += len(loaded.unmapped)

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fingate-run")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--rate-start", default="20230101")
    parser.add_argument("--rate-end", default=dt.date.today().strftime("%Y%m%d"))
    parser.add_argument("--years", default="2023,2024,2025")
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    summary = run(
        data_dir=args.data_dir,
        env_path=args.env,
        rate_start=args.rate_start,
        rate_end=args.rate_end,
        years=tuple(int(y) for y in args.years.split(",")),
        now=now,
    )

    print(f"금리 적재      {summary.rate_rows:>7,}행")
    print(f"재무지표 적재  {summary.financial_rows:>7,}행")
    print(f"미매핑 기록    {summary.unmapped_rows:>7,}건")
    print(f"계약 차단      {summary.blocked_requests:>7,}건")
    print(f"승인 대기 생성 {summary.staged_exceptions:>7,}건")
    print("서빙 상태:")
    for series_id, state in sorted(summary.serving_states.items()):
        print(f"  {series_id:<30}{state}")

    warehouse = Warehouse(args.data_dir / "fingate.duckdb")
    print(f"\n서빙 테이블 {len(warehouse.serving_rows()):,}행")
    if summary.staged_exceptions:
        print("\n승인 대기 건이 있습니다:  uv run python -m fingate.gate.cli list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
