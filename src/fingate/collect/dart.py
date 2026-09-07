"""DART 수집기.

두 엔드포인트를 다룬다.

- corpCode.xml: 전체 등록 법인 목록. 정상 3.6MB zip, 오류 시 150바이트 XML을
  같은 HTTP 200으로 반환한다(실측). 크기와 시그니처를 함께 검사한다.
- fnlttSinglAcnt.json: 단일회사 주요계정 재무제표.

조회 결과 없음(status 013)도 계약 위반으로 다룬다. 빈 성공으로 처리하면
결측이 조용히 하류로 퍼지고, 나중에 그 회사의 지표가 왜 비었는지 알 수 없다.
"""

import io
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contracts.response import verify_dart_json, verify_dart_zip
from .ecos import ContractViolation
from .raw_store import RawRecord, RawStore

BASE_URL = "https://opendart.fss.or.kr/api"
DEFAULT_TIMEOUT_SECONDS = 90
# 정상 응답은 약 3.6MB, 오류 응답은 150바이트다. 그 사이 어디든 임계로 쓸 수
# 있으나 부분 수신도 걸러내도록 넉넉히 잡는다.
DEFAULT_MIN_ZIP_BYTES = 100_000


class ReportCode(StrEnum):
    """DART 보고서 구분."""

    Q1 = "11013"
    HALF = "11012"
    Q3 = "11014"
    ANNUAL = "11011"


@dataclass(frozen=True)
class CorpEntry:
    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str

    @property
    def is_listed(self) -> bool:
        return bool(self.stock_code.strip())


@dataclass(frozen=True)
class CorpCodeResult:
    entries: list[CorpEntry]
    raw: RawRecord


@dataclass(frozen=True)
class FinancialResult:
    rows: list[dict[str, Any]]
    raw: RawRecord


def _urlopen_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        return response.read()


class DartCollector:
    def __init__(
        self,
        api_key: str,
        raw_store: RawStore,
        fetch: Callable[[str], bytes] = _urlopen_fetch,
    ) -> None:
        self._api_key = api_key
        self._raw_store = raw_store
        self._fetch = fetch

    def _url(self, endpoint: str, params: dict[str, str]) -> str:
        query = urllib.parse.urlencode({"crtfc_key": self._api_key, **params})
        return f"{BASE_URL}/{endpoint}?{query}"

    def collect_corp_codes(
        self,
        listed_only: bool = False,
        min_zip_bytes: int = DEFAULT_MIN_ZIP_BYTES,
    ) -> CorpCodeResult:
        body = self._fetch(self._url("corpCode.xml", {}))
        verdict = verify_dart_zip(body, min_bytes=min_zip_bytes)
        raw = self._raw_store.put(
            source="dart",
            endpoint="corpCode.xml",
            params={"listed_only": listed_only},
            body=body,
            ok=verdict.ok,
            verdict_code=verdict.code,
        )
        if not verdict.ok:
            raise ContractViolation(verdict.code or "UNKNOWN", verdict.message or "", raw)

        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml_bytes = archive.read(archive.namelist()[0])
        root = ElementTree.fromstring(xml_bytes)

        entries = [
            CorpEntry(
                corp_code=(item.findtext("corp_code") or "").strip(),
                corp_name=(item.findtext("corp_name") or "").strip(),
                stock_code=(item.findtext("stock_code") or "").strip(),
                modify_date=(item.findtext("modify_date") or "").strip(),
            )
            for item in root.findall("list")
        ]
        if listed_only:
            entries = [entry for entry in entries if entry.is_listed]
        if not entries:
            raise ContractViolation("NO_ROWS", "corp code archive contains no entries", raw)
        return CorpCodeResult(entries=entries, raw=raw)

    def collect_financials(
        self, corp_code: str, year: int, report_code: ReportCode
    ) -> FinancialResult:
        params = {
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": report_code.value,
        }
        body = self._fetch(self._url("fnlttSinglAcnt.json", params))
        verdict = verify_dart_json(body)
        raw = self._raw_store.put(
            source="dart",
            endpoint="fnlttSinglAcnt",
            params=params,
            body=body,
            ok=verdict.ok,
            verdict_code=verdict.code,
        )
        if not verdict.ok:
            raise ContractViolation(verdict.code or "UNKNOWN", verdict.message or "", raw)

        rows = json.loads(body.decode("utf-8")).get("list")
        if not isinstance(rows, list) or not rows:
            raise ContractViolation("NO_ROWS", "financial payload has no list", raw)
        return FinancialResult(rows=rows, raw=raw)
