"""Integration tests for database resilience and circuit breaker (Issue #1299).

Tests simulate database outages and verify that the application recovers
gracefully. Requires a running PostgreSQL instance for DB tests.
"""

from __future__ import annotations

import asyncio
import os

import pytest

_db_url = os.getenv("NEXUS_DATABASE_URL", "")
_requires_pg = pytest.mark.skipif(
    not _db_url.startswith("postgresql"),
    reason="Requires PostgreSQL (set NEXUS_DATABASE_URL)",
)


@_requires_pg
@pytest.mark.integration
@pytest.mark.postgres
class TestDatabaseResilience:
    """Verify application recovery after database interruptions."""

    def test_app_recovers_after_db_restart(self):
        """After a connection failure, subsequent sessions succeed (pool_pre_ping)."""
        from sqlalchemy import text

        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(db_url=_db_url, create_tables=False)

        # Initial successful query
        session = store.session_factory()
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1
        session.close()

        # Simulate stale connection by disposing pool
        store.engine.dispose()

        # Next session should auto-reconnect via pool_pre_ping
        session2 = store.session_factory()
        result2 = session2.execute(text("SELECT 1")).scalar()
        assert result2 == 1
        session2.close()
        store.close()


@pytest.mark.integration
class TestCircuitBreakerResilience:
    """Verify circuit breaker opens/closes correctly during outages."""

    async def test_circuit_breaker_opens_during_outage(self):
        """Circuit breaker opens after repeated failures via call()."""
        from nexus.core.exceptions import CircuitOpenError
        from nexus.rebac.circuit_breaker import (
            AsyncCircuitBreaker,
            CircuitBreakerConfig,
            CircuitState,
        )

        cb = AsyncCircuitBreaker(
            name="test_db",
            config=CircuitBreakerConfig(
                failure_threshold=3,
                success_threshold=2,
                reset_timeout=1.0,
                failure_window=10.0,
            ),
        )

        async def _fail() -> None:
            raise ConnectionError("simulated DB failure")

        # Accumulate failures
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await cb.call(_fail)

        assert cb.state == CircuitState.OPEN

        # Further calls should be rejected immediately
        with pytest.raises(CircuitOpenError):
            await cb.call(_fail)

    async def test_circuit_breaker_closes_after_recovery(self):
        """Circuit breaker closes after enough successful calls in HALF_OPEN."""
        from nexus.rebac.circuit_breaker import (
            AsyncCircuitBreaker,
            CircuitBreakerConfig,
            CircuitState,
        )

        cb = AsyncCircuitBreaker(
            name="test_db_recovery",
            config=CircuitBreakerConfig(
                failure_threshold=2,
                success_threshold=2,
                reset_timeout=0.1,  # Short timeout for test
                failure_window=10.0,
            ),
        )

        async def _fail() -> None:
            raise ConnectionError("simulated DB failure")

        async def _succeed() -> int:
            return 1

        # Open the breaker
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await cb.call(_fail)
        assert cb.state == CircuitState.OPEN

        # Wait for reset timeout → HALF_OPEN
        await asyncio.sleep(0.2)
        assert cb.state == CircuitState.HALF_OPEN

        # Record successes to close
        await cb.call(_succeed)
        await cb.call(_succeed)
        assert cb.state == CircuitState.CLOSED
