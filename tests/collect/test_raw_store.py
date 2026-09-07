"""원본 보존과 계보 테스트.

핵심 불변식: 자격 증명은 저장된 어디에도 남지 않는다. 경로에도, 메타데이터에도,
오류 메시지에도. 원본 응답은 계보 추적과 재현 가능한 백필을 위해 지우지 않는다.
"""

import json

import pytest

from fingate.collect.raw_store import REDACTED, RawStore


def test_stores_body_and_returns_record(tmp_path):
    store = RawStore(tmp_path)
    record = store.put(
        source="ecos",
        endpoint="StatisticTableList",
        params={"start": "1", "end": "5"},
        body=b'{"ok":true}',
    )
    assert record.byte_size == 11
    assert record.path.exists()
    assert record.path.read_bytes() == b'{"ok":true}'


def test_records_sha256_for_integrity(tmp_path):
    record = RawStore(tmp_path).put("ecos", "X", {}, b"abc")
    assert record.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_request_ids_are_unique(tmp_path):
    store = RawStore(tmp_path)
    first = store.put("ecos", "X", {}, b"a")
    second = store.put("ecos", "X", {}, b"a")
    assert first.request_id != second.request_id
    assert first.path != second.path


def test_api_key_is_never_written_to_metadata(tmp_path):
    store = RawStore(tmp_path)
    record = store.put(
        source="dart",
        endpoint="fnlttSinglAcnt",
        params={"crtfc_key": "SECRET_KEY_VALUE", "corp_code": "00113058"},
        body=b'{"status":"000"}',
    )
    meta = json.loads(record.meta_path.read_text(encoding="utf-8"))
    assert meta["params"]["crtfc_key"] == REDACTED
    assert meta["params"]["corp_code"] == "00113058"
    assert "SECRET_KEY_VALUE" not in record.meta_path.read_text(encoding="utf-8")


def test_api_key_is_never_written_to_path(tmp_path):
    record = RawStore(tmp_path).put(
        "ecos", "StatisticSearch", {"api_key": "SECRET_KEY_VALUE"}, b"{}"
    )
    assert "SECRET_KEY_VALUE" not in str(record.path)
    assert "SECRET_KEY_VALUE" not in str(record.meta_path)


@pytest.mark.parametrize(
    "name", ["crtfc_key", "api_key", "ECOS_API_KEY", "authKey", "serviceKey", "token", "password"]
)
def test_credential_like_names_are_redacted(tmp_path, name):
    record = RawStore(tmp_path).put("ecos", "X", {name: "SECRET"}, b"{}")
    meta = json.loads(record.meta_path.read_text(encoding="utf-8"))
    assert meta["params"][name] == REDACTED


def test_metadata_carries_lineage_fields(tmp_path):
    record = RawStore(tmp_path).put("ecos", "StatisticSearch", {"code": "722Y001"}, b"{}")
    meta = json.loads(record.meta_path.read_text(encoding="utf-8"))
    assert meta["request_id"] == record.request_id
    assert meta["source"] == "ecos"
    assert meta["endpoint"] == "StatisticSearch"
    assert meta["sha256"] == record.sha256
    assert meta["byte_size"] == 2
    assert meta["received_at"].endswith("+00:00")


def test_failed_response_is_still_preserved(tmp_path):
    """계약 위반 응답도 버리지 않는다. 무엇이 왜 거부됐는지 추적해야 한다."""
    store = RawStore(tmp_path)
    record = store.put(
        "ecos", "X", {}, b'{"RESULT":{"CODE":"INFO-100"}}', verdict_code="INFO-100", ok=False
    )
    meta = json.loads(record.meta_path.read_text(encoding="utf-8"))
    assert meta["ok"] is False
    assert meta["verdict_code"] == "INFO-100"
    assert record.path.exists()


def test_can_be_read_back_by_request_id(tmp_path):
    store = RawStore(tmp_path)
    record = store.put("ecos", "X", {}, b"payload")
    assert store.get_body(record.request_id) == b"payload"


def test_unknown_request_id_raises(tmp_path):
    with pytest.raises(KeyError, match="unknown request_id"):
        RawStore(tmp_path).get_body("does-not-exist")
