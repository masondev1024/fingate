"""요청 페이싱 테스트.

DART는 순간 요청이 몰리면 status=101(부적절한 접근)로 일시 차단한다.
2026-09-07 실측: 25건을 연속 호출한 뒤 모든 요청이 101로 바뀌었다.
HTTP는 여전히 200이다.
"""

import pytest

from fingate.collect.pacing import Pacer, RetryPolicy, retry_on


def test_pacer_waits_between_calls():
    slept: list[float] = []
    now = iter([0.0, 0.1])
    pacer = Pacer(min_interval_seconds=0.5, sleep=slept.append, monotonic=lambda: next(now))
    pacer.wait()  # 첫 호출은 대기하지 않는다
    pacer.wait()
    assert slept == [pytest.approx(0.4, abs=1e-6)]


def test_pacer_does_not_wait_when_interval_already_passed():
    slept: list[float] = []
    now = iter([0.0, 10.0])
    pacer = Pacer(min_interval_seconds=0.5, sleep=slept.append, monotonic=lambda: next(now))
    pacer.wait()
    pacer.wait()
    assert slept == []


def test_retry_returns_first_success():
    calls = {"n": 0}

    def operation():
        calls["n"] += 1
        return "ok"

    result = retry_on(operation, should_retry=lambda _: False, policy=RetryPolicy(attempts=3))
    assert result == "ok"
    assert calls["n"] == 1


def test_retry_retries_only_flagged_failures():
    attempts: list[int] = []

    class Throttled(Exception):
        pass

    def operation():
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise Throttled("101")
        return "ok"

    slept: list[float] = []
    result = retry_on(
        operation,
        should_retry=lambda exc: isinstance(exc, Throttled),
        policy=RetryPolicy(attempts=5, base_delay_seconds=1.0),
        sleep=slept.append,
    )
    assert result == "ok"
    assert len(attempts) == 3
    assert slept == [1.0, 2.0]  # 지수 백오프


def test_retry_reraises_when_not_retryable():
    def operation():
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        retry_on(operation, should_retry=lambda _: False, policy=RetryPolicy(attempts=3))


def test_retry_gives_up_after_attempts():
    class Throttled(Exception):
        pass

    def operation():
        raise Throttled("101")

    with pytest.raises(Throttled):
        retry_on(
            operation,
            should_retry=lambda exc: isinstance(exc, Throttled),
            policy=RetryPolicy(attempts=2, base_delay_seconds=0.0),
            sleep=lambda _: None,
        )


def test_policy_rejects_non_positive_attempts():
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy(attempts=0)
