"""DuckDB 적재 테스트.

재실행해도 중복이 생기지 않아야 한다(멱등). 백필과 재수집이 일상이므로
멱등하지 않으면 같은 기간을 두 번 돌린 순간 집계가 조용히 틀어진다.
"""

import datetime as dt

import pytest

from fingate.contracts.schema import Observation
from fingate.warehouse.store import Warehouse

NOW = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)


def _obs(day: int, value: float = 3.5, request_id: str = "req-1") -> Observation:
    return Observation(
        "base_rate_daily",
        dt.date(2026, 9, day),
        f"202609{day:02d}",
        value,
        "percent_per_annum",
        request_id,
    )


FINANCIAL_ROWS = [
    {"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": "147,900,000,000,000"},
    {"sj_div": "BS", "account_nm": "보험계약부채", "thstrm_amount": "120,000,000,000,000"},
    {"sj_div": "IS", "account_nm": "당기순이익(손실)", "thstrm_amount": "800,000,000,000"},
    {"sj_div": "BS", "account_nm": "파생상품자산", "thstrm_amount": "1,000,000"},
]


@pytest.fixture
def warehouse() -> Warehouse:
    return Warehouse()


# --- 금리 관측 ---


def test_loads_observations(warehouse):
    result = warehouse.load_observations([_obs(3), _obs(4)], loaded_at=NOW)
    assert result.inserted == 2
    assert warehouse.count("bronze_rate_observation") == 2


def test_reloading_the_same_period_does_not_duplicate(warehouse):
    warehouse.load_observations([_obs(3), _obs(4)], loaded_at=NOW)
    warehouse.load_observations([_obs(3), _obs(4)], loaded_at=NOW)
    assert warehouse.count("bronze_rate_observation") == 2


def test_reloading_updates_the_value_and_lineage(warehouse):
    warehouse.load_observations([_obs(3, 3.5, "req-1")], loaded_at=NOW)
    warehouse.load_observations([_obs(3, 2.75, "req-2")], loaded_at=NOW)
    row = warehouse.query(
        "SELECT value, request_id FROM bronze_rate_observation WHERE period = DATE '2026-09-03'"
    )[0]
    assert row["value"] == 2.75
    assert row["request_id"] == "req-2"


def test_keeps_series_separate(warehouse):
    other = Observation(
        "ktb_3y_daily", dt.date(2026, 9, 3), "20260903", 3.2, "percent_per_annum", "req-1"
    )
    warehouse.load_observations([_obs(3), other], loaded_at=NOW)
    assert warehouse.count("bronze_rate_observation") == 2


# --- 재무 ---


def test_loads_only_mapped_accounts(warehouse):
    result = warehouse.load_financials(
        corp_code="00113058",
        corp_name="한화생명",
        bsns_year=2023,
        report_code="11011",
        rows=FINANCIAL_ROWS,
        request_id="req-1",
        loaded_at=NOW,
    )
    assert result.inserted == 3
    assert warehouse.count("bronze_financial_indicator") == 3


def test_unmapped_accounts_are_recorded_not_dropped(warehouse):
    """조용히 버리면 그 회사 지표가 왜 비었는지 추적할 수 없다."""
    result = warehouse.load_financials(
        corp_code="00113058",
        corp_name="한화생명",
        bsns_year=2023,
        report_code="11011",
        rows=FINANCIAL_ROWS,
        request_id="req-1",
        loaded_at=NOW,
    )
    assert result.unmapped == ["BS/파생상품자산"]
    assert warehouse.count("bronze_unmapped_account") == 1


def test_parses_amounts_with_thousand_separators(warehouse):
    warehouse.load_financials(
        corp_code="00113058",
        corp_name="한화생명",
        bsns_year=2023,
        report_code="11011",
        rows=FINANCIAL_ROWS,
        request_id="req-1",
        loaded_at=NOW,
    )
    row = warehouse.query(
        "SELECT amount FROM bronze_financial_indicator WHERE indicator_id = 'total_assets'"
    )[0]
    assert row["amount"] == 147_900_000_000_000


def test_reloading_financials_is_idempotent(warehouse):
    for _ in range(2):
        warehouse.load_financials(
            corp_code="00113058",
            corp_name="한화생명",
            bsns_year=2023,
            report_code="11011",
            rows=FINANCIAL_ROWS,
            request_id="req-1",
            loaded_at=NOW,
        )
    assert warehouse.count("bronze_financial_indicator") == 3
    assert warehouse.count("bronze_unmapped_account") == 1


def test_non_numeric_amount_is_recorded_as_unmapped(warehouse):
    rows = [{"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": "-"}]
    result = warehouse.load_financials(
        corp_code="00113058",
        corp_name="한화생명",
        bsns_year=2023,
        report_code="11011",
        rows=rows,
        request_id="req-1",
        loaded_at=NOW,
    )
    assert result.inserted == 0
    assert result.unmapped == ["BS/자산총계"]


# --- 분기 집계 ---


def test_aggregates_daily_rates_into_quarters(warehouse):
    observations = [
        Observation(
            "base_rate_daily", dt.date(2026, 1, 5), "20260105", 3.0, "percent_per_annum", "r"
        ),
        Observation(
            "base_rate_daily", dt.date(2026, 2, 5), "20260205", 3.5, "percent_per_annum", "r"
        ),
        Observation(
            "base_rate_daily", dt.date(2026, 4, 5), "20260405", 2.0, "percent_per_annum", "r"
        ),
    ]
    warehouse.load_observations(observations, loaded_at=NOW)
    quarters = {(q["year"], q["quarter"]): q for q in warehouse.quarterly_rates()}
    assert quarters[(2026, 1)]["avg_value"] == pytest.approx(3.25)
    assert quarters[(2026, 1)]["observation_count"] == 2
    assert quarters[(2026, 2)]["avg_value"] == pytest.approx(2.0)


def test_quarterly_aggregation_is_per_series(warehouse):
    warehouse.load_observations(
        [
            _obs(3, 3.5),
            Observation(
                "ktb_3y_daily", dt.date(2026, 9, 3), "20260903", 2.9, "percent_per_annum", "r"
            ),
        ],
        loaded_at=NOW,
    )
    series_ids = {q["series_id"] for q in warehouse.quarterly_rates()}
    assert series_ids == {"base_rate_daily", "ktb_3y_daily"}


# --- 지속성 ---


def test_persists_to_disk(tmp_path):
    path = tmp_path / "fingate.duckdb"
    Warehouse(path).load_observations([_obs(3)], loaded_at=NOW)
    assert Warehouse(path).count("bronze_rate_observation") == 1


def test_writes_are_visible_to_a_second_connection_after_close(tmp_path):
    """앞선 연결이 열려 있으면 CLI 같은 별도 연결에서 쓰기가 보이지 않는다."""
    path = tmp_path / "fingate.duckdb"
    first = Warehouse(path)
    first.load_observations([_obs(3)], loaded_at=NOW)
    first.close()
    assert Warehouse(path).count("bronze_rate_observation") == 1


def test_can_be_used_as_a_context_manager(tmp_path):
    path = tmp_path / "fingate.duckdb"
    with Warehouse(path) as warehouse:
        warehouse.load_observations([_obs(3)], loaded_at=NOW)
    assert Warehouse(path).count("bronze_rate_observation") == 1
