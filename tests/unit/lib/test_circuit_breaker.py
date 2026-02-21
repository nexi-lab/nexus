"""Unit tests for ``nexus.lib.circuit_breaker`` — shared CB base (Issue #2125)."""

import asyncio
from unittest.mock import patch

import pytest

from nexus.lib.circuit_breaker import CircuitBreakerBase, CircuitState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TestBreaker(CircuitBreakerBase):
    """Concrete subclass for testing (no domain-specific behaviour)."""

    async def record_failure(self) -> None:
        await self._record_failure()

    async def record_success(self) -> None:
        await self._record_success()


def _make(
    failure_threshold: int = 3,
    success_threshold: int = 2,
    reset_timeout: float = 1.0,
) -> _TestBreaker:
    return _TestBreaker(
        failure_threshold=failure_threshold,
        success_threshold=success_threshold,
        reset_timeout=reset_timeout,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCircuitBreakerBase:
    """Tests for CircuitBreakerBase state machine."""

    def test_initial_state_is_closed(self) -> None:
        cb = _make()
        assert cb.current_state is CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0

    @pytest.mark.asyncio
    async def test_failures_below_threshold_stay_closed(self) -> None:
        cb = _make(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.current_state is CircuitState.CLOSED
        assert cb.failure_count == 2

    @pytest.mark.asyncio
    async def test_failures_at_threshold_transition_to_open(self) -> None:
        cb = _make(failure_threshold=3)
        for _ in range(3):
            await cb.record_failure()
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_rejects_immediately(self) -> None:
        cb = _make(failure_threshold=1, reset_timeout=999.0)
        await cb.record_failure()
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_transitions_to_half_open_after_timeout(self) -> None:
        cb = _make(failure_threshold=1, reset_timeout=0.5)
        await cb.record_failure()
        assert cb.current_state is CircuitState.OPEN

        # Fast-forward time past reset_timeout
        opened_at = cb._opened_at
        assert opened_at is not None
        with patch("nexus.lib.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = opened_at + 0.6
            assert cb.current_state is CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_transitions_to_closed(self) -> None:
        cb = _make(failure_threshold=1, success_threshold=2, reset_timeout=0.01)
        await cb.record_failure()
        assert cb.current_state is CircuitState.OPEN

        # Wait for reset timeout
        await asyncio.sleep(0.02)
        assert cb.current_state is CircuitState.HALF_OPEN

        # Record enough successes to close
        await cb.record_success()
        await cb.record_success()
        assert cb.current_state is CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_half_open_failure_returns_to_open(self) -> None:
        cb = _make(failure_threshold=1, reset_timeout=0.01)
        await cb.record_failure()
        await asyncio.sleep(0.02)
        assert cb.current_state is CircuitState.HALF_OPEN

        await cb.record_failure()
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_resets_failure_count_in_closed(self) -> None:
        cb = _make(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.failure_count == 2

        await cb.record_success()
        assert cb.failure_count == 0
        assert cb.current_state is CircuitState.CLOSED

    def test_lock_free_state_read(self) -> None:
        """current_state property does not acquire the asyncio lock."""
        cb = _make()
        # Access current_state outside an event loop — should not need lock
        assert cb.current_state is CircuitState.CLOSED
        # Verify the lock is not locked (it was never acquired)
        assert not cb._lock.locked()

    @pytest.mark.asyncio
    async def test_concurrent_transitions(self) -> None:
        """Multiple async tasks competing to trip the breaker."""
        cb = _make(failure_threshold=5, reset_timeout=0.01)

        async def fail_once() -> None:
            await cb.record_failure()

        # Fire 10 concurrent failures — should trip at 5
        await asyncio.gather(*[fail_once() for _ in range(10)])
        assert cb.current_state is CircuitState.OPEN
        # failure_count should be at least threshold
        assert cb.failure_count >= 5
