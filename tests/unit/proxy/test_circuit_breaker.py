"""Unit tests for AsyncCircuitBreaker."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from nexus.proxy.circuit_breaker import AsyncCircuitBreaker, CircuitState


@pytest.fixture
def breaker() -> AsyncCircuitBreaker:
    return AsyncCircuitBreaker(
        failure_threshold=3,
        recovery_timeout=1.0,
        half_open_max_calls=1,
    )


class TestAsyncCircuitBreaker:
    @pytest.mark.asyncio
    async def test_starts_closed(self, breaker: AsyncCircuitBreaker) -> None:
        assert breaker.state is CircuitState.CLOSED
        assert not breaker.is_open

    @pytest.mark.asyncio
    async def test_opens_after_threshold_failures(self, breaker: AsyncCircuitBreaker) -> None:
        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state is CircuitState.OPEN
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_stays_closed_below_threshold(self, breaker: AsyncCircuitBreaker) -> None:
        for _ in range(2):
            await breaker.record_failure()
        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self, breaker: AsyncCircuitBreaker) -> None:
        await breaker.record_failure()
        await breaker.record_failure()
        await breaker.record_success()
        # Should be able to tolerate 2 more failures without opening
        await breaker.record_failure()
        await breaker.record_failure()
        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_transitions_to_half_open_after_timeout(
        self, breaker: AsyncCircuitBreaker
    ) -> None:
        # Open the circuit
        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

        # Simulate time passing beyond recovery_timeout
        with patch("nexus.proxy.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 2.0
            assert breaker.state is CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_closes(self, breaker: AsyncCircuitBreaker) -> None:
        # Open the circuit
        for _ in range(3):
            await breaker.record_failure()

        # Transition to HALF_OPEN via time
        with patch("nexus.proxy.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 2.0
            assert breaker.state is CircuitState.HALF_OPEN
            await breaker.record_success()

        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self, breaker: AsyncCircuitBreaker) -> None:
        # Open the circuit
        for _ in range(3):
            await breaker.record_failure()

        # Transition to HALF_OPEN via time
        with patch("nexus.proxy.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 2.0
            assert breaker.state is CircuitState.HALF_OPEN
            await breaker.record_failure()

        assert breaker.state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_allow_request_closed(self, breaker: AsyncCircuitBreaker) -> None:
        assert await breaker.allow_request() is True

    @pytest.mark.asyncio
    async def test_allow_request_open(self, breaker: AsyncCircuitBreaker) -> None:
        for _ in range(3):
            await breaker.record_failure()
        assert await breaker.allow_request() is False

    @pytest.mark.asyncio
    async def test_allow_request_half_open_limited(self, breaker: AsyncCircuitBreaker) -> None:
        for _ in range(3):
            await breaker.record_failure()

        with patch("nexus.proxy.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 2.0
            # First call allowed (half_open_max_calls=1)
            assert await breaker.allow_request() is True
            # Second call denied
            assert await breaker.allow_request() is False

    @pytest.mark.asyncio
    async def test_reset(self, breaker: AsyncCircuitBreaker) -> None:
        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state is CircuitState.OPEN
        await breaker.reset()
        assert breaker.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_concurrent_access_safe(self, breaker: AsyncCircuitBreaker) -> None:
        """Multiple coroutines racing on record_failure should not corrupt state."""

        async def fail_n(n: int) -> None:
            for _ in range(n):
                await breaker.record_failure()

        await asyncio.gather(fail_n(2), fail_n(2))
        # At least 3 failures happened, so it should be open
        assert breaker.state is CircuitState.OPEN
