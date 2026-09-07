"""조회 CLI 테스트."""

import datetime as dt

import pytest

from fingate.contracts.schema import Observation
from fingate.warehouse.query_cli import main
from fingate.warehouse.store import Warehouse

NOW = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def db(tmp_path):
    """두 분기를 넣는다. 변화율과 상관계수는 이전 분기가 있어야 계산된다."""
    path = tmp_path / "fingate.duckdb"
    warehouse = Warehouse(path)
    warehouse.load_observations(
        [
            Observation(
                "base_rate_daily", dt.date(2024, 2, 15), "x", 3.5, "percent_per_annum", "req-1"
            ),
            Observation(
                "base_rate_daily", dt.date(2024, 5, 15), "x", 3.0, "percent_per_annum", "req-1"
            ),
        ],
        loaded_at=NOW,
    )
    for report_code, assets in (("11013", "100"), ("11012", "110")):
        warehouse.load_financials(
            corp_code="00113058",
            corp_name="한화생명",
            bsns_year=2024,
            report_code=report_code,
            rows=[{"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": assets}],
            request_id="req-1",
            loaded_at=NOW,
        )
    warehouse.close()
    return path


def test_serving_lists_rows(db, capsys):
    assert main(["--db", str(db), "serving"]) == 0
    assert "한화생명" in capsys.readouterr().out


def test_change_view_runs(db, capsys):
    assert main(["--db", str(db), "change"]) == 0
    assert "한화생명" in capsys.readouterr().out


def test_change_filters_by_company(db, capsys):
    assert main(["--db", str(db), "change", "--corp", "삼성생명"]) == 0
    assert "없습니다" in capsys.readouterr().out


def test_freshness_uses_as_of(db, capsys):
    assert main(["--db", str(db), "freshness", "--as-of", "2024-05-20"]) == 0
    out = capsys.readouterr().out
    assert "base_rate_daily" in out
    assert "5" in out


def test_sensitivity_warns_about_small_samples(db, capsys):
    """표본이 적을 때 상관계수를 그대로 내보내면 과신된다."""
    assert main(["--db", str(db), "sensitivity"]) == 0
    assert "표본" in capsys.readouterr().out


def test_missing_database_exits_nonzero(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "nope.duckdb"), "serving"]) == 1
    assert "not found" in capsys.readouterr().err
