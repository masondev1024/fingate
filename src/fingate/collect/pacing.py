"""요청 페이싱과 재시도.

DART는 순간 요청이 몰리면 status=101(부적절한 접근)로 일시 차단한다.
2026-09-07 실측: 25건을 빠르게 연속 호출한 뒤 모든 요청이 101이 되었고,
HTTP는 여전히 200이었다. 상태 코드로는 감지할 수 없다.

시간 함수를 주입 가능하게 두어 테스트가 실제로 잠들지 않게 한다.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass


class Pacer:
    """호출 사이에 최소 간격을 강제한다."""

    def __init__(
        self,
        min_interval_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative")
        self._min_interval = min_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float | None = None

    def wait(self) -> None:
        """직전 호출로부터 최소 간격이 지나도록 잠든다.

        시간 조회는 호출당 한 번만 한다. 잠든 시간은 계산으로 더한다.
        두 번 조회하면 sleep 정확도에 따라 간격이 흔들린다.
        """
        now = self._monotonic()
        if self._last_call is not None:
            remaining = self._min_interval - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
                now += remaining
        self._last_call = now


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")

    def delay_for(self, attempt_index: int) -> float:
        """지수 백오프. 일시 차단은 시간이 지나야 풀리므로 간격을 늘린다."""
        return self.base_delay_seconds * (2**attempt_index)


def retry_on[T](
    operation: Callable[[], T],
    should_retry: Callable[[Exception], bool],
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """재시도 대상으로 표시된 실패만 다시 시도한다.

    영구 실패(잘못된 인증키, 존재하지 않는 코드)를 재시도하면 차단만 길어진다.
    """
    last_error: Exception | None = None
    for attempt_index in range(policy.attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - should_retry가 분류를 맡는다
            if not should_retry(exc):
                raise
            last_error = exc
            if attempt_index < policy.attempts - 1:
                sleep(policy.delay_for(attempt_index))
    assert last_error is not None
    raise last_error
