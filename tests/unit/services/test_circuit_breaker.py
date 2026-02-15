"""Tests for AsyncCircuitBreaker state machine (Issue #726).

Covers all circuit breaker states, transitions, configuration validation,
sliding window, excluded exceptions, metrics, and concurrency.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

from nexus.core.exceptions import CircuitOpenError
from nexus.services.permissions.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)

# =========================================================================
# Helpers
# =========================================================================


def _make_cb(**overrides) -> AsyncCircuitBreaker:
    """Create a circuit breaker with test-friendly defaults."""
    defaults = {
        "failure_threshold": 3,
        "success_threshold": 2,
        "reset_timeout": 0.1,  # 100ms for fast tests
        "failure_window": 5.0,
    }
    defaults.update(overrides)
    return AsyncCircuitBreaker(
        name="test",
        config=CircuitBreakerConfig(**defaults),
    )


async def _fail_with(cb: AsyncCircuitBreaker, exc: BaseException) -> None:
    """Trigger a single failure on the circuit breaker."""
    failing = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)):
        await cb.call(failing)


async def _succeed(cb: AsyncCircuitBreaker, value: str = "ok") -> str:
    """Trigger a single success on the circuit breaker."""
    fn = AsyncMock(return_value=value)
    return await cb.call(fn)


# =========================================================================
# Tests
# =========================================================================


class TestCircuitBreakerStateMachine:
    """Tests 1-8: Core state transitions."""

    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self):
        cb = _make_cb()
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_keeps_closed(self):
        cb = _make_cb()
        await _succeed(cb)
        await _succeed(cb)
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_below_threshold_stays_closed(self):
        cb = _make_cb(failure_threshold=3)
        await _fail_with(cb, OperationalError("conn lost", None, None))
        await _fail_with(cb, OperationalError("conn lost", None, None))
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_at_threshold_opens_circuit(self):
        cb = _make_cb(failure_threshold=3)
        for _ in range(3):
            await _fail_with(cb, OperationalError("conn lost", None, None))
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_rejects_without_calling_fn(self):
        cb = _make_cb(failure_threshold=1)
        await _fail_with(cb, OperationalError("boom", None, None))
        assert cb.state == CircuitState.OPEN

        fn = AsyncMock(return_value="should not run")
        with pytest.raises(CircuitOpenError) as exc_info:
            await cb.call(fn)
        fn.assert_not_called()
        assert exc_info.value.service_name == "test"

    @pytest.mark.asyncio
    async def test_open_transitions_to_half_open_after_timeout(self):
        cb = _make_cb(failure_threshold=1, reset_timeout=0.05)
        await _fail_with(cb, OperationalError("boom", None, None))
        assert cb.state == CircuitState.OPEN

        await asyncio.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_threshold_closes(self):
        cb = _make_cb(failure_threshold=1, success_threshold=2, reset_timeout=0.05)
        await _fail_with(cb, OperationalError("boom", None, None))
        await asyncio.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN

        await _succeed(cb)
        await _succeed(cb)
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self):
        cb = _make_cb(failure_threshold=1, reset_timeout=0.05)
        await _fail_with(cb, OperationalError("boom", None, None))
        await asyncio.sleep(0.06)
        assert cb.state == CircuitState.HALF_OPEN

        await _fail_with(cb, OperationalError("boom again", None, None))
        # Internal state should be OPEN (reopened)
        assert cb._state == CircuitState.OPEN
        # open_count should have incremented (reopened)
        assert cb.open_count == 2


class TestExcludedExceptions:
    """Test 9: Business logic exceptions don't trip the breaker."""

    @pytest.mark.asyncio
    async def test_excluded_exceptions_dont_trip(self):
        cb = _make_cb(failure_threshold=1)
        # ValueError is a business exception, should NOT trip
        for _ in range(5):
            with pytest.raises(ValueError):
                await cb.call(AsyncMock(side_effect=ValueError("bad input")))
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_infrastructure_exceptions_trip(self):
        """OperationalError, InterfaceError, TimeoutError, ConnectionError, OSError should trip."""
        for exc_type in [
            lambda: OperationalError("conn", None, None),
            lambda: InterfaceError("iface", None, None),
            lambda: TimeoutError("timeout"),
            lambda: ConnectionError("conn refused"),
            lambda: OSError("io error"),
        ]:
            cb = _make_cb(failure_threshold=1)
            await _fail_with(cb, exc_type())
            assert cb.state == CircuitState.OPEN, f"{exc_type} should trip the breaker"


class TestSlidingWindow:
    """Test 10: Failures outside the time window don't count."""

    @pytest.mark.asyncio
    async def test_failure_window_sliding(self):
        cb = _make_cb(failure_threshold=3, failure_window=0.1)
        # Two failures
        await _fail_with(cb, OperationalError("1", None, None))
        await _fail_with(cb, OperationalError("2", None, None))
        assert cb.state == CircuitState.CLOSED

        # Wait for window to expire
        await asyncio.sleep(0.15)

        # One more failure — but old ones expired, so still under threshold
        await _fail_with(cb, OperationalError("3", None, None))
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 1  # Only the recent failure


class TestMetrics:
    """Test 11: Metrics attributes are tracked correctly."""

    @pytest.mark.asyncio
    async def test_metrics_tracking(self):
        cb = _make_cb(failure_threshold=2)
        await _succeed(cb)
        assert cb.success_count == 1

        await _fail_with(cb, OperationalError("1", None, None))
        assert cb.failure_count == 1
        assert cb.last_failure_time is not None

        await _fail_with(cb, OperationalError("2", None, None))
        assert cb.state == CircuitState.OPEN
        assert cb.open_count == 1


class TestConcurrency:
    """Test 12: Multiple concurrent coroutines see consistent state."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_consistent_state(self):
        cb = _make_cb(failure_threshold=3)
        # 10 concurrent successes shouldn't break anything
        results = await asyncio.gather(*[cb.call(AsyncMock(return_value=i)) for i in range(10)])
        assert len(results) == 10
        assert cb.state == CircuitState.CLOSED
        assert cb.success_count == 10


class TestConfigValidation:
    """Test 13: Invalid configs raise ValueError."""

    def test_failure_threshold_zero(self):
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreakerConfig(failure_threshold=0)

    def test_success_threshold_zero(self):
        with pytest.raises(ValueError, match="success_threshold"):
            CircuitBreakerConfig(success_threshold=0)

    def test_reset_timeout_zero(self):
        with pytest.raises(ValueError, match="reset_timeout"):
            CircuitBreakerConfig(reset_timeout=0)

    def test_failure_window_negative(self):
        with pytest.raises(ValueError, match="failure_window"):
            CircuitBreakerConfig(failure_window=-1)


class TestStateProperty:
    """Test 14: State property handles OPEN→HALF_OPEN transition correctly."""

    @pytest.mark.asyncio
    async def test_state_property_returns_half_open_when_timeout_expired(self):
        cb = _make_cb(failure_threshold=1, reset_timeout=0.05)
        await _fail_with(cb, OperationalError("boom", None, None))

        # Internal state is OPEN
        assert cb._state == CircuitState.OPEN
        # Property should still show OPEN before timeout
        assert cb.state == CircuitState.OPEN

        await asyncio.sleep(0.06)
        # Property should now show HALF_OPEN (but internal _state hasn't changed)
        assert cb.state == CircuitState.HALF_OPEN


class TestReset:
    """Test 15: Manual reset clears all counters."""

    @pytest.mark.asyncio
    async def test_reset_clears_all_counters(self):
        cb = _make_cb(failure_threshold=1)
        await _fail_with(cb, OperationalError("boom", None, None))
        assert cb.state == CircuitState.OPEN
        assert cb.open_count == 1

        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0
        assert cb.open_count == 0
        assert cb.last_failure_time is None


class TestDescribe:
    """Test 16: Human-readable description for debugging."""

    def test_describe_returns_human_readable(self):
        cb = _make_cb()
        desc = cb.describe()
        assert "test" in desc
        assert "closed" in desc
        assert "failures=" in desc
