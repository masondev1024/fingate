"""실험 하네스 테스트.

측정 도구 자체가 틀리면 결과가 무의미하다. 특히 검증기 라우팅이 틀리면
차단은 되더라도 어느 계층이 잡았는지 귀속이 어긋난다.
"""

import datetime as dt
import json

import pytest

from fingate.collect.series import series_by_id
from fingate.experiment.catalog import CATALOG, PRE_REGISTERED, Layer, SourceKind
from fingate.experiment.harness import Harness, summarize

SPEC = series_by_id("base_rate_daily")
AS_OF = dt.date(2026, 9, 7)
NOW = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)


def _row(time: str = "20260904", value: str = "3.0") -> dict:
    return {
        "STAT_CODE": SPEC.stat_code,
        "ITEM_CODE1": SPEC.item_code,
        "UNIT_NAME": SPEC.source_unit,
        "TIME": time,
        "DATA_VALUE": value,
    }


CLEAN_ROWS = [_row("20260902"), _row("20260903"), _row("20260904")]
CLEAN_BODY = json.dumps({"StatisticSearch": {"row": CLEAN_ROWS}}).encode()


def _harness(tmp_path) -> Harness:
    return Harness(SPEC, CLEAN_ROWS, tmp_path, AS_OF, NOW)


def _corruption(corruption_id: str):
    return next(c for c in CATALOG if c.corruption_id == corruption_id)


def test_catalog_ids_are_unique():
    ids = [c.corruption_id for c in CATALOG]
    assert len(ids) == len(set(ids))


def test_pre_registered_subset_matches_spec_count():
    """설계 문서에 사전 등록한 것은 9종이다."""
    assert len(PRE_REGISTERED) == 9
    assert all(c.pre_registered for c in PRE_REGISTERED)


def test_zip_corruption_is_routed_to_the_zip_verifier():
    assert _corruption("dart_zip_error_xml").source_kind is SourceKind.DART_ZIP


def test_throttle_corruption_is_routed_to_the_json_verifier():
    """101은 JSON 엔드포인트에서 온다. zip 검증기로 잡으면 귀속이 틀린다."""
    assert _corruption("dart_throttled").source_kind is SourceKind.DART_JSON


def test_clean_data_reaches_serving(tmp_path):
    """게이트가 정상 데이터를 막으면 측정 전체가 무의미하다."""
    assert _harness(tmp_path)._serve(CLEAN_ROWS, "clean")[0] is True


def test_error_payload_is_blocked_at_response_layer(tmp_path):
    body = json.dumps({"RESULT": {"CODE": "INFO-100", "MESSAGE": "bad"}}).encode()
    result = _harness(tmp_path).run(_corruption("ecos_auth_error"), body, [])
    assert result.gated_reached_serving is False
    assert result.blocked_by_layer == "response"
    assert result.detail == "INFO-100"


def test_naive_pipeline_serves_the_error_payload(tmp_path):
    """대조군이 오염을 통과시켜야 비교가 성립한다."""
    body = json.dumps({"RESULT": {"CODE": "INFO-100", "MESSAGE": "bad"}}).encode()
    result = _harness(tmp_path).run(_corruption("ecos_auth_error"), body, [])
    assert result.naive_reached_serving is True
    assert result.naive_outcome == "served_bad_data"


def test_zip_error_crashes_naive_pipeline_rather_than_serving(tmp_path):
    """zip 오류는 조용한 오염이 아니라 크래시다. 구분해서 기록한다."""
    body = b"<?xml version='1.0'?><result><status>010</status></result>"
    result = _harness(tmp_path).run(_corruption("dart_zip_error_xml"), body, [])
    assert result.naive_outcome == "crashed"
    assert result.naive_reached_serving is False


def test_outlier_is_blocked_at_quality_layer(tmp_path):
    rows = CLEAN_ROWS[:-1] + [_row("20260904", "999")]
    result = _harness(tmp_path).run(_corruption("value_outlier"), CLEAN_BODY, rows)
    assert result.blocked_by_layer == "quality"


def test_swapped_series_is_blocked_at_schema_layer(tmp_path):
    rows = [dict(row, ITEM_CODE1="9999999") for row in CLEAN_ROWS]
    result = _harness(tmp_path).run(_corruption("series_swapped"), CLEAN_BODY, rows)
    assert result.blocked_by_layer == "schema"


def test_expected_layer_matches_actual_for_response_corruptions(tmp_path):
    corruption = _corruption("ecos_unknown_table")
    body = json.dumps({"RESULT": {"CODE": "INFO-200", "MESSAGE": "none"}}).encode()
    result = _harness(tmp_path).run(corruption, body, [])
    assert result.blocked_by_layer == str(Layer.RESPONSE)


def test_summarize_counts_rates_and_outcomes(tmp_path):
    harness = _harness(tmp_path)
    body = json.dumps({"RESULT": {"CODE": "INFO-100", "MESSAGE": "bad"}}).encode()
    results = [
        harness.run(_corruption("ecos_auth_error"), body, []),
        harness.run(
            _corruption("value_outlier"), CLEAN_BODY, CLEAN_ROWS[:-1] + [_row("20260904", "999")]
        ),
    ]
    summary = summarize(results)
    assert summary["total"] == 2
    assert summary["gated_reached"] == 0
    assert summary["gated_rate"] == pytest.approx(0.0)
    assert summary["blocked_by_layer"] == {"response": 1, "quality": 1}
