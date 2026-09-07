"""분석 뷰 테스트.

윈도우 함수로 분기 대비 변화를 계산한다. 첫 분기는 이전 값이 없으므로
변화율이 NULL이어야 한다. 0으로 채우면 "변화 없음"으로 오인된다.
"""

import datetime as dt

import pytest

from fingate.contracts.schema import Observation
from fingate.warehouse.store import Warehouse

NOW = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)


def _financial(warehouse, corp, name, year, report, assets, income):
    warehouse.load_financials(
        corp_code=corp,
        corp_name=name,
        bsns_year=year,
        report_code=report,
        rows=[
            {"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": str(assets)},
            {
                "sj_div": "IS",
                "account_nm": "당기순이익(손실)",
                "thstrm_amount": str(income),
                "thstrm_add_amount": "",
            },
        ],
        request_id="req-1",
        loaded_at=NOW,
    )


def _rate(warehouse, year, month, value):
    warehouse.load_observations(
        [
            Observation(
                "base_rate_daily",
                dt.date(year, month, 15),
                "x",
                value,
                "percent_per_annum",
                "req-1",
            )
        ],
        loaded_at=NOW,
    )


@pytest.fixture
def warehouse() -> Warehouse:
    wh = Warehouse()
    # 2024 Q1~Q3, 자산 증가 / 금리 하락
    for quarter, (report, month, assets, rate) in enumerate(
        [("11013", 2, 100, 3.5), ("11012", 5, 110, 3.0), ("11014", 8, 121, 2.5)], start=1
    ):
        _financial(wh, "00113058", "한화생명", 2024, report, assets, 10 * quarter)
        _rate(wh, 2024, month, rate)
    return wh


def test_computes_quarter_over_quarter_delta(warehouse):
    rows = {
        r["quarter"]: r
        for r in warehouse.query(
            "SELECT * FROM analytics_insurer_quarterly_change WHERE year = 2024"
        )
    }
    assert rows[2]["assets_qoq_delta"] == 10
    assert rows[3]["assets_qoq_delta"] == 11


def test_first_quarter_has_null_change_not_zero(warehouse):
    """이전 값이 없는 것과 변화가 0인 것은 다르다."""
    first = next(
        r
        for r in warehouse.query(
            "SELECT * FROM analytics_insurer_quarterly_change WHERE year = 2024 AND quarter = 1"
        )
    )
    assert first["assets_qoq_delta"] is None
    assert first["assets_qoq_pct"] is None


def test_computes_percentage_change(warehouse):
    row = next(
        r
        for r in warehouse.query(
            "SELECT * FROM analytics_insurer_quarterly_change WHERE year = 2024 AND quarter = 2"
        )
    )
    assert row["assets_qoq_pct"] == pytest.approx(10.0)


def test_tracks_rate_change_alongside(warehouse):
    row = next(
        r
        for r in warehouse.query(
            "SELECT * FROM analytics_insurer_quarterly_change WHERE year = 2024 AND quarter = 2"
        )
    )
    assert row["rate_qoq_delta"] == pytest.approx(-0.5)


def test_change_is_partitioned_per_company(warehouse):
    """다른 회사의 값이 섞이면 변화율이 완전히 틀린다."""
    for report, assets in [("11013", 500), ("11012", 400), ("11014", 300)]:
        _financial(warehouse, "00126256", "삼성생명", 2024, report, assets, 5)
    rows = warehouse.query(
        """
        SELECT corp_name, quarter, assets_qoq_delta
        FROM analytics_insurer_quarterly_change
        WHERE year = 2024 AND quarter = 2 ORDER BY corp_name
        """
    )
    by_name = {r["corp_name"]: r["assets_qoq_delta"] for r in rows}
    assert by_name["한화생명"] == 10
    assert by_name["삼성생명"] == -100


def test_sensitivity_reports_sample_size(warehouse):
    """표본 수를 함께 내지 않으면 상관계수가 과신된다."""
    rows = warehouse.query("SELECT * FROM analytics_rate_sensitivity")
    row = next(r for r in rows if r["corp_name"] == "한화생명")
    assert row["quarters_compared"] == 2
    assert "correlation" in row


def test_freshness_view_reports_age_against_sla(warehouse):
    rows = {r["series_id"]: r for r in warehouse.query("SELECT * FROM analytics_series_freshness")}
    row = rows["base_rate_daily"]
    assert row["latest_period"] == dt.date(2024, 8, 15)
    assert row["observation_count"] == 3
