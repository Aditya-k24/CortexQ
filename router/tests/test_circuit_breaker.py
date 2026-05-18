import time
import pytest
from circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def make_cb(**kwargs) -> CircuitBreaker:
    defaults = dict(name="test", failure_threshold=3, success_threshold=2, timeout=1.0)
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)


class TestCircuitBreakerInitial:
    def test_starts_closed(self):
        cb = make_cb()
        assert cb.state == "closed"

    def test_check_passes_when_closed(self):
        cb = make_cb()
        cb.check()  # no exception

    def test_state_numeric_closed_is_zero(self):
        cb = make_cb()
        assert cb.state_numeric() == 0

    def test_is_open_false_when_closed(self):
        cb = make_cb()
        assert cb.is_open() is False


class TestCircuitBreakerTransitions:
    def test_closed_to_open_on_threshold(self):
        cb = make_cb(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"

    def test_does_not_open_before_threshold(self):
        cb = make_cb(failure_threshold=3)
        for _ in range(2):
            cb.record_failure()
        assert cb.state == "closed"

    def test_open_rejects_check(self):
        cb = make_cb(failure_threshold=1)
        cb.record_failure()
        with pytest.raises(CircuitOpenError):
            cb.check()

    def test_open_to_half_open_after_timeout(self):
        cb = make_cb(failure_threshold=1, timeout=0.05)
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.1)
        cb.check()  # transitions to half_open — no exception
        assert cb.state == "half_open"

    def test_half_open_to_closed_on_success_threshold(self):
        cb = make_cb(failure_threshold=1, timeout=0.05, success_threshold=2)
        cb.record_failure()
        time.sleep(0.1)
        cb.check()  # -> half_open
        cb.record_success()
        assert cb.state == "half_open"
        cb.record_success()
        assert cb.state == "closed"

    def test_half_open_to_open_on_failure(self):
        cb = make_cb(failure_threshold=1, timeout=0.05)
        cb.record_failure()
        time.sleep(0.1)
        cb.check()  # -> half_open
        cb.record_failure()
        assert cb.state == "open"

    def test_still_open_before_timeout(self):
        cb = make_cb(failure_threshold=1, timeout=60.0)
        cb.record_failure()
        with pytest.raises(CircuitOpenError):
            cb.check()
        assert cb.state == "open"

    def test_state_numeric_open_is_one(self):
        cb = make_cb(failure_threshold=1)
        cb.record_failure()
        assert cb.state_numeric() == 1

    def test_state_numeric_half_open_is_two(self):
        cb = make_cb(failure_threshold=1, timeout=0.05)
        cb.record_failure()
        time.sleep(0.1)
        cb.check()
        assert cb.state_numeric() == 2


class TestCircuitBreakerReset:
    def test_reset_clears_state(self):
        cb = make_cb(failure_threshold=1)
        cb.record_failure()
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"
        cb.check()  # no exception

    def test_success_in_closed_decrements_failures(self):
        cb = make_cb(failure_threshold=5)
        cb.record_failure()
        cb.record_failure()
        assert cb._failure_count == 2
        cb.record_success()
        assert cb._failure_count == 1

    def test_success_does_not_go_below_zero(self):
        cb = make_cb()
        cb.record_success()
        assert cb._failure_count == 0


class TestCircuitBreakerRepr:
    def test_repr_contains_name_and_state(self):
        cb = make_cb(name="claude")
        r = repr(cb)
        assert "claude" in r
        assert "closed" in r
