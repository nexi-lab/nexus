"""Integration tests for the resiliency layer (Issue #1366).

Verifies the full pipeline: config → manager → decorator → CB state transitions
→ health check reporting — using fast timings suitable for CI.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.resiliency import (
    CircuitBreakerOpenError,
    CircuitBreakerPolicy,
    CircuitState,
    ResiliencyConfig,
    ResiliencyManager,
    RetryPolicy,
    TargetBinding,
    TimeoutPolicy,
    with_resiliency,
)


@pytest.fixture()
def fast_manager() -> ResiliencyManager:
    """Manager with fast CB (threshold=2, timeout=0.1s) for integration testing."""
    config = ResiliencyConfig(
        timeouts={
            "default": TimeoutPolicy(seconds=5.0),
            "fast": TimeoutPolicy(seconds=0.05),
        },
        retries={
            "default": RetryPolicy(max_retries=1, min_wait=0.01, max_interval=0.02),
        },
        circuit_breakers={
            "default": CircuitBreakerPolicy(
                failure_threshold=2,
                success_threshold=1,
                timeout=0.1,
            ),
        },
        targets={
            "backend": TargetBinding(
                timeout="default",
                retry="default",
                circuit_breaker="default",
            ),
        },
    )
    return ResiliencyManager(config=config)


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_trip_and_recover(self, fast_manager: ResiliencyManager) -> None:
        """Simulate failures → CB trips → fail-fast → half-open recovery.

        The CB wraps the retry loop, so each decorated call that exhausts
        retries counts as 1 CB failure (the final exception propagates
        through __aexit__). With failure_threshold=2, we need 2 such calls.
        """
        call_count = 0

        @with_resiliency(target="backend", manager=fast_manager)
        async def backend_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 10:
                raise ConnectionError("backend down")
            return "recovered"

        # Phase 1: Two decorated calls that exhaust retries → 2 CB failures → trip
        with pytest.raises(ConnectionError):
            await backend_call()
        with pytest.raises(ConnectionError):
            await backend_call()

        # Phase 2: CB is open, calls fail fast
        health = fast_manager.health_check()
        assert health["status"] == "degraded"
        assert health["circuit_breakers"]["default"]["state"] == "open"

        with pytest.raises(CircuitBreakerOpenError):
            await backend_call()

        # Phase 3: Wait for half-open
        await asyncio.sleep(0.15)
        cb = fast_manager.get_breaker("default")
        assert cb.current_state is CircuitState.HALF_OPEN

        # Phase 4: Successful probe → CLOSED
        call_count = 100  # ensure success path
        result = await backend_call()
        assert result == "recovered"

        health = fast_manager.health_check()
        assert health["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_reflects_state_transitions(self, fast_manager: ResiliencyManager) -> None:
        """Health check accurately reflects CB state at each transition."""
        cb = fast_manager.get_breaker("default")

        # Initially healthy
        assert fast_manager.health_check()["status"] == "ok"

        # Trip the CB
        for _ in range(2):
            with pytest.raises(ConnectionError):
                async with cb:
                    raise ConnectionError("fail")

        health = fast_manager.health_check()
        assert health["status"] == "degraded"
        assert health["circuit_breakers"]["default"]["failure_count"] == 2
        assert health["circuit_breakers"]["default"]["last_failure_time"] is not None
