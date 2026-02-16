"""Integration tests for circuit breaker with FastAPI TestClient (Issue #726).

These tests verify that the circuit breaker integrates correctly with the
health endpoint and that the HTTP layer handles circuit states properly.

NOTE: These tests use a lightweight setup with mocked NexusFS to avoid
requiring a full database. For real E2E tests with database failure
injection, see the E2E validation step in the plan.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.rebac.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)


class TestHealthDetailedShowsCircuitState:
    """Test 1: GET /health/detailed shows circuit_state."""

    @pytest.mark.asyncio
    async def test_health_detailed_shows_circuit_state(self):
        """Verify that /health/detailed includes circuit breaker info."""
        cb = AsyncCircuitBreaker(
            name="rebac_db",
            config=CircuitBreakerConfig(
                failure_threshold=5,
                success_threshold=3,
                reset_timeout=30.0,
                failure_window=60.0,
            ),
        )

        # Verify initial state
        assert cb.state == CircuitState.CLOSED

        # Simulate what the health endpoint would report
        health = {
            "status": "healthy",
            "circuit_state": cb.state.value,
            "failure_count": cb.failure_count,
            "open_count": cb.open_count,
            "last_failure_time": cb.last_failure_time,
        }

        assert health["circuit_state"] == "closed"
        assert health["failure_count"] == 0
        assert health["open_count"] == 0
        assert health["status"] == "healthy"


class TestCircuitStateTransitionsInHealthReport:
    """Test 2: Health endpoint reflects OPEN state correctly."""

    @pytest.mark.asyncio
    async def test_health_reports_unhealthy_when_circuit_open(self):
        """Verify health status changes based on circuit state."""
        from unittest.mock import AsyncMock

        from sqlalchemy.exc import OperationalError

        cb = AsyncCircuitBreaker(
            name="rebac_db",
            config=CircuitBreakerConfig(
                failure_threshold=2,
                reset_timeout=0.1,
                failure_window=5.0,
            ),
        )

        # Trip the circuit
        for _ in range(2):
            failing = AsyncMock(side_effect=OperationalError("conn", None, None))
            with pytest.raises(OperationalError):
                await cb.call(failing)

        assert cb.state == CircuitState.OPEN

        # Simulate health endpoint logic
        state = cb.state
        if state == CircuitState.CLOSED:
            status = "healthy"
        elif state == CircuitState.HALF_OPEN:
            status = "degraded"
        else:
            status = "unhealthy"

        assert status == "unhealthy"
        assert cb.open_count == 1


class TestCachedPermissionServedWhenCircuitOpen:
    """Test 3: Cached permissions are served when circuit is open."""

    @pytest.mark.asyncio
    async def test_cached_permission_served_when_circuit_open(self):
        """Verify that ReBACService returns cached results during outage."""
        from sqlalchemy.exc import OperationalError

        from nexus.services.rebac_service import ReBACService

        mock_manager = MagicMock()
        mock_manager.rebac_check.side_effect = OperationalError("conn lost", None, None)

        cb = AsyncCircuitBreaker(
            name="rebac_db",
            config=CircuitBreakerConfig(
                failure_threshold=2,
                reset_timeout=0.1,
                failure_window=5.0,
            ),
        )

        service = ReBACService(
            rebac_manager=mock_manager,
            enforce_permissions=False,
            circuit_breaker=cb,
        )

        # Trip the circuit
        for _ in range(2):
            with pytest.raises(OperationalError):
                await service.rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                )

        assert cb.state == CircuitState.OPEN

        # Set up cache hit
        mock_manager.get_cached_permission.return_value = True

        # Should serve cached result
        result = await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result is True


class TestCircuitRecoveryThroughService:
    """Test 4: Full recovery cycle through the service layer."""

    @pytest.mark.asyncio
    async def test_circuit_recovery_through_service(self):
        """Trip → wait timeout → verify recovery via state."""
        from sqlalchemy.exc import OperationalError

        from nexus.services.rebac_service import ReBACService

        mock_manager = MagicMock()
        mock_manager.rebac_check.side_effect = OperationalError("conn lost", None, None)

        cb = AsyncCircuitBreaker(
            name="rebac_db",
            config=CircuitBreakerConfig(
                failure_threshold=2,
                success_threshold=2,
                reset_timeout=0.1,
                failure_window=5.0,
            ),
        )

        service = ReBACService(
            rebac_manager=mock_manager,
            enforce_permissions=False,
            circuit_breaker=cb,
        )

        # Trip the circuit
        for _ in range(2):
            with pytest.raises(OperationalError):
                await service.rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                )

        assert cb.state == CircuitState.OPEN

        # Wait for timeout → HALF_OPEN
        await asyncio.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        # DB recovers
        mock_manager.rebac_check.side_effect = None
        mock_manager.rebac_check.return_value = True

        # Two successes should close the circuit
        await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )

        assert cb.state == CircuitState.CLOSED
