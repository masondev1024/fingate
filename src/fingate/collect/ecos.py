"""ECOS 수집기.

계약을 통과하지 못한 응답은 데이터로 돌려주지 않고 예외로 끊는다(fail-closed).
부분 데이터를 반환하면 호출자가 그것을 정상으로 오인해 적재한다.

전송 계층을 주입 가능하게 두어 테스트가 네트워크에 의존하지 않게 한다.
"""

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..contracts.response import verify_ecos
from .raw_store import RawRecord, RawStore

BASE_URL = "https://ecos.bok.or.kr/api"
SERVICE = "StatisticSearch"
DEFAULT_TIMEOUT_SECONDS = 40


class ContractViolation(Exception):
    """응답이 계약을 위반했다. 자격 증명은 메시지에 담지 않는다."""

    def __init__(self, code: str, message: str, raw: RawRecord) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.raw = raw


@dataclass(frozen=True)
class EcosRequest:
    stat_code: str
    cycle: str
    start_period: str
    end_period: str
    item_code: str | None = None
    start_row: int = 1
    end_row: int = 1000

    def path_suffix(self) -> str:
        """인증키를 제외한 경로. 로그와 메타데이터에 쓸 수 있다."""
        parts = [
            "json",
            "kr",
            str(self.start_row),
            str(self.end_row),
            self.stat_code,
            self.cycle,
            self.start_period,
            self.end_period,
        ]
        if self.item_code:
            parts.append(self.item_code)
        return "/".join(parts)


@dataclass(frozen=True)
class CollectResult:
    rows: list[dict[str, Any]]
    total_count: int
    raw: RawRecord


def _urlopen_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        return response.read()


class EcosCollector:
    def __init__(
        self,
        api_key: str,
        raw_store: RawStore,
        fetch: Callable[[str], bytes] = _urlopen_fetch,
    ) -> None:
        self._api_key = api_key
        self._raw_store = raw_store
        self._fetch = fetch

    def collect(self, request: EcosRequest) -> CollectResult:
        url = f"{BASE_URL}/{SERVICE}/{self._api_key}/{request.path_suffix()}"
        body = self._fetch(url)

        verdict = verify_ecos(body)
        raw = self._raw_store.put(
            source="ecos",
            endpoint=SERVICE,
            # 인증키는 파라미터로 넘기지 않는다. 넘기면 마스킹에 의존하게 된다.
            params={"path": request.path_suffix(), "stat_code": request.stat_code},
            body=body,
            ok=verdict.ok,
            verdict_code=verdict.code,
        )
        if not verdict.ok:
            raise ContractViolation(verdict.code or "UNKNOWN", verdict.message or "", raw)

        payload = json.loads(body.decode("utf-8")).get(SERVICE, {})
        rows = payload.get("row")
        if not isinstance(rows, list) or not rows:
            raise ContractViolation("NO_ROWS", f"{SERVICE} payload has no row list", raw)

        return CollectResult(
            rows=rows, total_count=int(payload.get("list_total_count", 0)), raw=raw
        )
