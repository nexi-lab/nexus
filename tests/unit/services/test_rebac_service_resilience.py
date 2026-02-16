"""Tests for ReBACService resilience with circuit breaker (Issue #726).

Covers failure scenarios, cache fallback, write operation behavior,
circuit recovery, and business exception pass-through.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from nexus.core.exceptions import CircuitOpenError
from nexus.rebac.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from nexus.services.rebac_service import ReBACService

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def mock_rebac_manager():
    """Create a mock EnhancedReBACManager."""
    mock = MagicMock()
    mock.rebac_check.return_value = True
    mock.rebac_expand.return_value = [("user", "alice")]
    mock.rebac_check_batch_fast.return_value = [True, False]
    # get_cached_permission returns None by default (cache miss)
    mock.get_cached_permission.return_value = None

    write_result = MagicMock()
    write_result.tuple_id = "tuple-123"
    write_result.revision = 42
    write_result.consistency_token = "token-abc"
    mock.rebac_write.return_value = write_result

    mock.rebac_delete.return_value = True
    return mock


@pytest.fixture
def circuit_breaker():
    """Create a circuit breaker with fast test-friendly config."""
    return AsyncCircuitBreaker(
        name="rebac_db_test",
        config=CircuitBreakerConfig(
            failure_threshold=3,
            success_threshold=2,
            reset_timeout=0.1,
            failure_window=5.0,
        ),
    )


@pytest.fixture
def service(mock_rebac_manager, circuit_breaker):
    """Create ReBACService with circuit breaker."""
    return ReBACService(
        rebac_manager=mock_rebac_manager,
        enforce_permissions=False,
        circuit_breaker=circuit_breaker,
    )


# =========================================================================
# Tests
# =========================================================================


class TestCircuitOpensAfterDBFailures:
    """Test 1: Circuit opens after repeated DB failures."""

    @pytest.mark.asyncio
    async def test_circuit_opens_after_db_failures(self, service, mock_rebac_manager):
        # Make rebac_check raise OperationalError
        mock_rebac_manager.rebac_check.side_effect = OperationalError("connection lost", None, None)

        # First 3 calls raise OperationalError (and trip the circuit)
        for _ in range(3):
            with pytest.raises(OperationalError):
                await service.rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                )

        # 4th call should raise CircuitOpenError (not OperationalError)
        with pytest.raises(CircuitOpenError):
            await service.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
            )


class TestFallbackReturnsCachedPermission:
    """Test 2: Circuit open + cache hit returns cached permission."""

    @pytest.mark.asyncio
    async def test_fallback_returns_cached_permission(self, service, mock_rebac_manager):
        # Trip the circuit
        mock_rebac_manager.rebac_check.side_effect = OperationalError("connection lost", None, None)
        for _ in range(3):
            with pytest.raises(OperationalError):
                await service.rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                )

        # Set up cache hit
        mock_rebac_manager.get_cached_permission.return_value = True

        # Should return cached result instead of raising
        result = await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result is True
        mock_rebac_manager.get_cached_permission.assert_called_once()


class TestCircuitOpenCacheMissRaises:
    """Test 3: Circuit open + cache miss raises CircuitOpenError."""

    @pytest.mark.asyncio
    async def test_circuit_open_cache_miss_raises(self, service, mock_rebac_manager):
        # Trip the circuit
        mock_rebac_manager.rebac_check.side_effect = OperationalError("connection lost", None, None)
        for _ in range(3):
            with pytest.raises(OperationalError):
                await service.rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                )

        # Cache miss (default: returns None)
        mock_rebac_manager.get_cached_permission.return_value = None

        with pytest.raises(CircuitOpenError):
            await service.rebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
            )


class TestWriteOperationsNoFallback:
    """Test 4: Write operations raise CircuitOpenError without cache fallback."""

    @pytest.mark.asyncio
    async def test_write_operations_no_fallback(self, service, mock_rebac_manager):
        # Trip the circuit
        mock_rebac_manager.rebac_write.side_effect = OperationalError("connection lost", None, None)
        for _ in range(3):
            with pytest.raises(OperationalError):
                await service.rebac_create(
                    subject=("user", "alice"),
                    relation="owner",
                    object=("file", "/doc.txt"),
                )

        # Next call should raise CircuitOpenError (no cache fallback for writes)
        with pytest.raises(CircuitOpenError):
            await service.rebac_create(
                subject=("user", "bob"),
                relation="viewer",
                object=("file", "/other.txt"),
            )

        # get_cached_permission should NOT be called for writes
        mock_rebac_manager.get_cached_permission.assert_not_called()


class TestCircuitRecovery:
    """Test 5: Full recovery cycle — failures → open → timeout → half-open → success → closed."""

    @pytest.mark.asyncio
    async def test_circuit_recovery(self, service, mock_rebac_manager, circuit_breaker):
        # Phase 1: Trip the circuit
        mock_rebac_manager.rebac_check.side_effect = OperationalError("connection lost", None, None)
        for _ in range(3):
            with pytest.raises(OperationalError):
                await service.rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                )
        assert circuit_breaker.state == CircuitState.OPEN

        # Phase 2: Wait for timeout → HALF_OPEN
        await asyncio.sleep(0.15)
        assert circuit_breaker.state == CircuitState.HALF_OPEN

        # Phase 3: DB recovers — successes close the circuit
        mock_rebac_manager.rebac_check.side_effect = None
        mock_rebac_manager.rebac_check.return_value = True

        result1 = await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result1 is True
        result2 = await service.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
        )
        assert result2 is True

        assert circuit_breaker.state == CircuitState.CLOSED


class TestBusinessExceptionsPassThrough:
    """Test 6: Business exceptions (ValueError) don't trip the circuit."""

    @pytest.mark.asyncio
    async def test_business_exceptions_pass_through(
        self, service, mock_rebac_manager, circuit_breaker
    ):
        # ValueError is a business exception — should pass through without tripping
        mock_rebac_manager.rebac_check.side_effect = ValueError("bad input")

        for _ in range(10):
            with pytest.raises(ValueError, match="bad input"):
                await service.rebac_check(
                    subject=("user", "alice"),
                    permission="read",
                    object=("file", "/doc.txt"),
                )

        # Circuit should still be CLOSED
        assert circuit_breaker.state == CircuitState.CLOSED
