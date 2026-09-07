"""DuckDB 적재.

재실행해도 중복이 생기지 않아야 한다. 백필과 재수집은 일상이고, 멱등하지
않으면 같은 기간을 두 번 돌린 순간 집계가 조용히 틀어진다. 자연키에
기본키를 걸고 upsert 한다.

매핑되지 않은 계정과 파싱 불가 금액은 버리지 않고 별도 테이블에 남긴다.
조용히 버리면 나중에 그 회사 지표가 왜 비었는지 추적할 수 없다.
"""

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from ..contracts.schema import Observation
from .indicators import map_account

SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS bronze_rate_observation (
        series_id  VARCHAR NOT NULL,
        period     DATE    NOT NULL,
        value      DOUBLE  NOT NULL,
        unit       VARCHAR NOT NULL,
        request_id VARCHAR NOT NULL,
        loaded_at  TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (series_id, period)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bronze_financial_indicator (
        corp_code    VARCHAR NOT NULL,
        corp_name    VARCHAR NOT NULL,
        bsns_year    INTEGER NOT NULL,
        report_code  VARCHAR NOT NULL,
        indicator_id VARCHAR NOT NULL,
        statement    VARCHAR NOT NULL,
        account_nm   VARCHAR NOT NULL,
        amount       HUGEINT NOT NULL,
        request_id   VARCHAR NOT NULL,
        loaded_at    TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (corp_code, bsns_year, report_code, indicator_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bronze_unmapped_account (
        corp_code   VARCHAR NOT NULL,
        bsns_year   INTEGER NOT NULL,
        report_code VARCHAR NOT NULL,
        statement   VARCHAR NOT NULL,
        account_nm  VARCHAR NOT NULL,
        reason      VARCHAR NOT NULL,
        request_id  VARCHAR NOT NULL,
        loaded_at   TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (corp_code, bsns_year, report_code, statement, account_nm)
    )
    """,
)


@dataclass
class LoadResult:
    inserted: int = 0
    unmapped: list[str] = field(default_factory=list)


def _parse_amount(raw: Any) -> int | None:
    """DART 금액은 천 단위 구분자가 있는 문자열이다. 빈 값과 '-'도 온다."""
    text = str(raw or "").replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


class Warehouse:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self._connection = duckdb.connect(str(path))
        for statement in SCHEMA_DDL:
            self._connection.execute(statement)

    def query(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        cursor = self._connection.execute(sql, parameters)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def count(self, table: str) -> int:
        # 테이블명은 코드 내부에서만 오므로 식별자 보간이 안전하다.
        return int(self._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def load_observations(
        self, observations: list[Observation], loaded_at: dt.datetime
    ) -> LoadResult:
        result = LoadResult()
        for observation in observations:
            self._connection.execute(
                """
                INSERT INTO bronze_rate_observation
                    (series_id, period, value, unit, request_id, loaded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (series_id, period) DO UPDATE SET
                    value = excluded.value,
                    unit = excluded.unit,
                    request_id = excluded.request_id,
                    loaded_at = excluded.loaded_at
                """,
                (
                    observation.series_id,
                    observation.period,
                    observation.value,
                    observation.unit,
                    observation.request_id,
                    loaded_at,
                ),
            )
            result.inserted += 1
        return result

    def load_financials(
        self,
        corp_code: str,
        corp_name: str,
        bsns_year: int,
        report_code: str,
        rows: list[dict[str, Any]],
        request_id: str,
        loaded_at: dt.datetime,
    ) -> LoadResult:
        result = LoadResult()
        for row in rows:
            statement = str(row.get("sj_div", ""))
            account_name = str(row.get("account_nm", "")).strip()
            indicator = map_account(statement, account_name)
            amount = _parse_amount(row.get("thstrm_amount"))

            if indicator is None or amount is None:
                reason = "UNMAPPED_ACCOUNT" if indicator is None else "AMOUNT_NOT_NUMERIC"
                result.unmapped.append(f"{statement}/{account_name}")
                self._connection.execute(
                    """
                    INSERT INTO bronze_unmapped_account
                        (corp_code, bsns_year, report_code, statement, account_nm,
                         reason, request_id, loaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (corp_code, bsns_year, report_code, statement, account_nm)
                    DO UPDATE SET
                        reason = excluded.reason,
                        request_id = excluded.request_id,
                        loaded_at = excluded.loaded_at
                    """,
                    (
                        corp_code,
                        bsns_year,
                        report_code,
                        statement,
                        account_name,
                        reason,
                        request_id,
                        loaded_at,
                    ),
                )
                continue

            self._connection.execute(
                """
                INSERT INTO bronze_financial_indicator
                    (corp_code, corp_name, bsns_year, report_code, indicator_id,
                     statement, account_nm, amount, request_id, loaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (corp_code, bsns_year, report_code, indicator_id) DO UPDATE SET
                    corp_name = excluded.corp_name,
                    account_nm = excluded.account_nm,
                    amount = excluded.amount,
                    request_id = excluded.request_id,
                    loaded_at = excluded.loaded_at
                """,
                (
                    corp_code,
                    corp_name,
                    bsns_year,
                    report_code,
                    indicator.indicator_id,
                    str(indicator.statement),
                    account_name,
                    amount,
                    request_id,
                    loaded_at,
                ),
            )
            result.inserted += 1
        return result

    def quarterly_rates(self) -> list[dict[str, Any]]:
        """일별·월별 금리를 분기 평균으로 집계한다.

        DART 재무가 분기 단위이므로 조인하려면 금리를 분기로 맞춰야 한다.
        관측 수를 함께 내보내 표본이 얇은 분기를 식별할 수 있게 한다.
        """
        return self.query(
            """
            SELECT
                series_id,
                CAST(year(period) AS INTEGER)    AS year,
                CAST(quarter(period) AS INTEGER) AS quarter,
                avg(value)                       AS avg_value,
                min(value)                       AS min_value,
                max(value)                       AS max_value,
                count(*)                         AS observation_count
            FROM bronze_rate_observation
            GROUP BY series_id, year, quarter
            ORDER BY series_id, year, quarter
            """
        )
