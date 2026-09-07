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
from .periods import quarter_of, resolve_amount

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
        quarter      INTEGER NOT NULL,
        indicator_id VARCHAR NOT NULL,
        statement    VARCHAR NOT NULL,
        account_nm   VARCHAR NOT NULL,
        amount       HUGEINT NOT NULL,
        period_kind  VARCHAR NOT NULL,
        cumulative_amount HUGEINT,
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
    """
    CREATE OR REPLACE VIEW silver_rate_quarterly AS
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
    """,
    """
    CREATE OR REPLACE VIEW serving_insurer_rate_context AS
    WITH rates AS (
        SELECT
            year, quarter,
            max(CASE WHEN series_id = 'base_rate_daily' THEN avg_value END) AS base_rate,
            max(CASE WHEN series_id = 'ktb_3y_daily'   THEN avg_value END) AS ktb_3y,
            max(CASE WHEN series_id = 'ktb_10y_daily'  THEN avg_value END) AS ktb_10y,
            max(CASE WHEN series_id = 'base_rate_daily' THEN observation_count END)
                AS rate_observations
        FROM silver_rate_quarterly
        GROUP BY year, quarter
    ),
    financials AS (
        SELECT
            corp_code, corp_name, bsns_year AS year, quarter,
            max(CASE WHEN indicator_id = 'total_assets'    THEN amount END) AS total_assets,
            max(CASE WHEN indicator_id = 'total_equity'    THEN amount END) AS total_equity,
            max(CASE WHEN indicator_id = 'insurance_contract_liabilities' THEN amount END)
                AS insurance_contract_liabilities,
            -- 손익은 분기치만 쓴다. 사업보고서는 연간 전체라 분기로 오인하면 안 된다.
            max(CASE WHEN indicator_id = 'net_income' AND period_kind = 'quarter'
                     THEN amount END) AS net_income_quarter,
            max(CASE WHEN indicator_id = 'net_income' AND period_kind = 'annual'
                     THEN amount END) AS net_income_annual
        FROM bronze_financial_indicator
        GROUP BY corp_code, corp_name, year, quarter
    ),
    -- 4분기 손익은 DART가 직접 주지 않는다. 사업보고서는 연간 전체이므로
    -- 연간에서 3분기 누적을 빼서 유도한다. 유도값임을 플래그로 표시한다.
    q3_cumulative AS (
        SELECT corp_code, bsns_year AS year,
               max(CASE WHEN indicator_id = 'net_income' AND quarter = 3
                        THEN cumulative_amount END) AS value
        FROM bronze_financial_indicator
        GROUP BY corp_code, bsns_year
    )
    SELECT
        f.corp_code, f.corp_name, f.year, f.quarter,
        f.total_assets, f.total_equity, f.insurance_contract_liabilities,
        coalesce(
            f.net_income_quarter,
            CASE WHEN f.quarter = 4 AND f.net_income_annual IS NOT NULL
                      AND c.value IS NOT NULL
                 THEN f.net_income_annual - c.value END
        ) AS net_income_quarter,
        (f.net_income_quarter IS NULL AND f.quarter = 4 AND f.net_income_annual IS NOT NULL
         AND c.value IS NOT NULL) AS net_income_is_derived,
        f.net_income_annual,
        r.base_rate, r.ktb_3y, r.ktb_10y, r.rate_observations
    FROM financials f
    LEFT JOIN rates r ON r.year = f.year AND r.quarter = f.quarter
    LEFT JOIN q3_cumulative c ON c.corp_code = f.corp_code AND c.year = f.year
    """,
    """
    CREATE OR REPLACE VIEW analytics_insurer_quarterly_change AS
    SELECT
        corp_code, corp_name, year, quarter,
        total_assets,
        insurance_contract_liabilities,
        net_income_quarter,
        base_rate,
        -- 이전 분기가 없으면 NULL로 둔다. 0으로 채우면 "변화 없음"으로 오인된다.
        total_assets - lag(total_assets) OVER w AS assets_qoq_delta,
        100.0 * (total_assets - lag(total_assets) OVER w)
            / nullif(lag(total_assets) OVER w, 0) AS assets_qoq_pct,
        base_rate - lag(base_rate) OVER w AS rate_qoq_delta,
        avg(net_income_quarter) OVER (
            PARTITION BY corp_code ORDER BY year, quarter
            ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
        ) AS net_income_4q_avg
    FROM serving_insurer_rate_context
    WINDOW w AS (PARTITION BY corp_code ORDER BY year, quarter)
    """,
    """
    CREATE OR REPLACE VIEW analytics_rate_sensitivity AS
    SELECT
        corp_name,
        count(*) AS quarters_compared,
        corr(rate_qoq_delta, assets_qoq_pct) AS correlation,
        avg(assets_qoq_pct)                  AS avg_assets_qoq_pct,
        avg(rate_qoq_delta)                  AS avg_rate_qoq_delta
    FROM analytics_insurer_quarterly_change
    WHERE rate_qoq_delta IS NOT NULL AND assets_qoq_pct IS NOT NULL
    GROUP BY corp_name
    """,
    """
    -- 신선도는 current_date가 아니라 명시적 as_of 기준으로 잰다.
    -- check_quality도 as_of를 받으므로 기준이 한 곳으로 모인다.
    CREATE OR REPLACE VIEW analytics_series_freshness AS
    SELECT
        series_id,
        max(period)   AS latest_period,
        min(period)   AS earliest_period,
        count(*)      AS observation_count
    FROM bronze_rate_observation
    GROUP BY series_id
    """,
)


@dataclass
class LoadResult:
    inserted: int = 0
    unmapped: list[str] = field(default_factory=list)


class Warehouse:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self._connection = duckdb.connect(str(path))
        for statement in SCHEMA_DDL:
            self._connection.execute(statement)

    def close(self) -> None:
        """연결을 닫는다.

        같은 파일에 두 번째 연결을 여는 경우(예: CLI가 파이프라인 결과를 읽을 때)
        앞선 연결이 열려 있으면 쓰기가 보이지 않는다.
        """
        self._connection.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

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
            amount, period_kind, cumulative = resolve_amount(
                statement, report_code, row.get("thstrm_amount"), row.get("thstrm_add_amount")
            )

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
                    (corp_code, corp_name, bsns_year, report_code, quarter, indicator_id,
                     statement, account_nm, amount, period_kind, cumulative_amount,
                     request_id, loaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (corp_code, bsns_year, report_code, indicator_id) DO UPDATE SET
                    corp_name = excluded.corp_name,
                    account_nm = excluded.account_nm,
                    amount = excluded.amount,
                    period_kind = excluded.period_kind,
                    cumulative_amount = excluded.cumulative_amount,
                    request_id = excluded.request_id,
                    loaded_at = excluded.loaded_at
                """,
                (
                    corp_code,
                    corp_name,
                    bsns_year,
                    report_code,
                    quarter_of(report_code),
                    indicator.indicator_id,
                    str(indicator.statement),
                    account_name,
                    amount,
                    str(period_kind),
                    cumulative,
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
        return self.query("SELECT * FROM silver_rate_quarterly ORDER BY series_id, year, quarter")

    def freshness(self, as_of: dt.date) -> list[dict[str, Any]]:
        """시계열별 신선도. 경과일은 호출자가 준 기준일로 계산한다."""
        return self.query(
            """
            SELECT
                f.series_id, f.latest_period, f.earliest_period, f.observation_count,
                CAST(? - f.latest_period AS INTEGER) AS age_days
            FROM analytics_series_freshness f
            ORDER BY f.series_id
            """,
            (as_of,),
        )

    def serving_rows(self) -> list[dict[str, Any]]:
        """금리 환경과 보험사 재무를 분기 축으로 조인한 서빙 테이블.

        손익은 분기치(period_kind='quarter')만 쓴다. 사업보고서의 손익은
        연간 전체라 분기로 오인하면 규모가 크게 부풀려진다.
        """
        return self.query(
            "SELECT * FROM serving_insurer_rate_context ORDER BY corp_name, year, quarter"
        )
