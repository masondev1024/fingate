"""스키마 계약 테스트.

거부된 행은 버리지 않고 사유와 함께 남긴다. 조용히 버리면 결측이
어디서 생겼는지 추적할 수 없다.
"""

import datetime as dt

import pytest

from fingate.collect.series import series_by_id
from fingate.contracts.schema import normalize_ecos_rows, parse_period

SPEC = series_by_id("base_rate_daily")
MONTHLY = series_by_id("bank_loan_avg_monthly")


def _row(**overrides):
    row = {
        "STAT_CODE": "722Y001",
        "ITEM_CODE1": "0101000",
        "ITEM_NAME1": "한국은행 기준금리",
        "UNIT_NAME": "연%",
        "TIME": "20240102",
        "DATA_VALUE": "3.5",
    }
    row.update(overrides)
    return row


# --- 기간 파싱 ---


@pytest.mark.parametrize(
    ("cycle", "raw", "expected"),
    [
        ("D", "20240102", dt.date(2024, 1, 2)),
        ("M", "202401", dt.date(2024, 1, 1)),
        ("Q", "2024Q2", dt.date(2024, 4, 1)),
        ("A", "2024", dt.date(2024, 1, 1)),
    ],
)
def test_parses_period_per_cycle(cycle, raw, expected):
    assert parse_period(raw, cycle) == expected


@pytest.mark.parametrize(
    ("cycle", "raw"), [("D", "202401"), ("M", "20240102"), ("Q", "2024"), ("D", "notadate")]
)
def test_rejects_period_not_matching_cycle(cycle, raw):
    with pytest.raises(ValueError, match="period"):
        parse_period(raw, cycle)


# --- 정규화 ---


def test_accepts_valid_row():
    result = normalize_ecos_rows(SPEC, [_row()], request_id="req-1")
    assert len(result.observations) == 1
    assert result.rejected == []
    obs = result.observations[0]
    assert obs.series_id == "base_rate_daily"
    assert obs.period == dt.date(2024, 1, 2)
    assert obs.value == 3.5
    assert obs.unit == SPEC.unit
    assert obs.request_id == "req-1"


def test_rejects_empty_value_without_dropping_it():
    """ECOS는 결측을 빈 문자열로 보낸다. 조용히 버리면 결측이 사라진다."""
    result = normalize_ecos_rows(SPEC, [_row(DATA_VALUE="")], request_id="req-1")
    assert result.observations == []
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "VALUE_NOT_NUMERIC"
    assert result.rejected[0].raw_period == "20240102"


def test_rejects_non_numeric_value():
    result = normalize_ecos_rows(SPEC, [_row(DATA_VALUE="N/A")], request_id="req-1")
    assert result.rejected[0].reason == "VALUE_NOT_NUMERIC"


def test_rejects_missing_required_field():
    row = _row()
    del row["TIME"]
    result = normalize_ecos_rows(SPEC, [row], request_id="req-1")
    assert result.rejected[0].reason == "MISSING_FIELD"


def test_rejects_unit_label_drift():
    """단위 라벨이 바뀌면 API 계약이 바뀐 것이다. 값 의미가 달라졌을 수 있다."""
    result = normalize_ecos_rows(SPEC, [_row(UNIT_NAME="%")], request_id="req-1")
    assert result.rejected[0].reason == "UNIT_MISMATCH"


def test_rejects_wrong_item_code():
    """요청한 항목과 다른 항목이 오면 시계열이 뒤바뀐 것이다."""
    result = normalize_ecos_rows(SPEC, [_row(ITEM_CODE1="9999999")], request_id="req-1")
    assert result.rejected[0].reason == "ITEM_MISMATCH"


def test_rejects_wrong_stat_code():
    result = normalize_ecos_rows(SPEC, [_row(STAT_CODE="999Y999")], request_id="req-1")
    assert result.rejected[0].reason == "STAT_MISMATCH"


def test_rejects_period_not_matching_declared_cycle():
    result = normalize_ecos_rows(SPEC, [_row(TIME="202401")], request_id="req-1")
    assert result.rejected[0].reason == "PERIOD_FORMAT"


def test_monthly_series_accepts_monthly_period():
    row = _row(STAT_CODE="121Y006", ITEM_CODE1="BECBLA01", TIME="202401", DATA_VALUE="5.04")
    result = normalize_ecos_rows(MONTHLY, [row], request_id="req-1")
    assert result.observations[0].period == dt.date(2024, 1, 1)


def test_partial_batch_keeps_good_rows_and_records_bad_ones():
    rows = [_row(), _row(TIME="20240103", DATA_VALUE=""), _row(TIME="20240104", DATA_VALUE="3.5")]
    result = normalize_ecos_rows(SPEC, rows, request_id="req-1")
    assert len(result.observations) == 2
    assert len(result.rejected) == 1
    assert result.acceptance_rate == pytest.approx(2 / 3)


def test_every_rejection_carries_lineage():
    result = normalize_ecos_rows(SPEC, [_row(DATA_VALUE="")], request_id="req-42")
    assert result.rejected[0].request_id == "req-42"
    assert result.rejected[0].series_id == "base_rate_daily"
