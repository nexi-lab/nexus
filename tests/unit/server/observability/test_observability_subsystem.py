"""Tests for ObservabilitySubsystem health_check, cleanup, and circuit breaker.

Issue #2072: Fill coverage gap for ObservabilitySubsystem lifecycle.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event

from nexus.core.config import ObservabilityConfig
from nexus.server.observability.observability_subsystem import (
    ObservabilitySubsystem,
    QueryObserver,
)


@pytest.fixture
def config() -> ObservabilityConfig:
    return ObservabilityConfig(
        enable_query_logging=True,
        enable_pool_metrics=True,
        slow_query_threshold_ms=100.0,
        max_listener_errors=3,
    )


@pytest.fixture
def observer(config: ObservabilityConfig) -> QueryObserver:
    return QueryObserver(config=config)


@pytest.fixture
def subsystem(config: ObservabilityConfig) -> ObservabilitySubsystem:
    return ObservabilitySubsystem(config=config)


class TestHealthCheck:
    """Tests for ObservabilitySubsystem.health_check()."""

    def test_health_check_ok_when_enabled(self, subsystem: ObservabilitySubsystem) -> None:
        result = subsystem.health_check()
        assert result["status"] == "ok"
        assert result["subsystem"] == "observability"
        assert result["query_observer_enabled"] is True

    def test_health_check_degraded_when_disabled(self, subsystem: ObservabilitySubsystem) -> None:
        # Trigger circuit breaker by forcing disabled state
        subsystem._observer._disabled = True
        result = subsystem.health_check()
        assert result["status"] == "degraded"
        assert result["query_observer_enabled"] is False


class TestCleanup:
    """Tests for ObservabilitySubsystem.cleanup()."""

    def test_cleanup_resets_counters(self, subsystem: ObservabilitySubsystem) -> None:
        # Simulate some activity
        subsystem._observer._total_queries = 42
        subsystem._observer._slow_queries = 5
        subsystem._observer._pool_checkouts = 10

        subsystem.cleanup()

        assert subsystem._observer.total_queries == 0
        assert subsystem._observer.slow_queries == 0
        assert subsystem._observer.pool_checkouts == 0

    def test_cleanup_removes_listeners(self, subsystem: ObservabilitySubsystem) -> None:
        # Use a real mock that tracks remove_listeners calls
        # (can't use a real engine because SQLAlchemy event.listen validates targets)
        mock_engine = MagicMock()
        # Manually add to engines list without calling instrument_engine
        # to avoid SQLAlchemy event validation
        subsystem._observer._engines.append(mock_engine)
        assert len(subsystem._observer.engines) == 1

        with patch.object(subsystem._observer, "remove_listeners") as mock_remove:
            subsystem.cleanup()
            mock_remove.assert_called_once_with(mock_engine)

        assert len(subsystem._observer.engines) == 0


class TestInstrumentEngine:
    """Tests for engine instrumentation."""

    def test_instrument_engine_attaches_listeners(self, config: ObservabilityConfig) -> None:
        subsystem = ObservabilitySubsystem(config=config)
        engine = MagicMock()
        engine.pool = MagicMock()

        with patch.object(event, "listen") as mock_listen:
            subsystem.instrument_engine(engine)

        # 2 query listeners + 4 pool listeners = 6 total
        assert mock_listen.call_count == 6
        assert len(subsystem._observer.engines) == 1


class TestCircuitBreaker:
    """Tests for circuit breaker auto-disable."""

    def test_circuit_breaker_auto_disables(self, observer: QueryObserver) -> None:
        # max_listener_errors=3, so after 3 errors it should disable
        assert not observer.disabled

        observer._record_error()
        assert not observer.disabled
        observer._record_error()
        assert not observer.disabled
        observer._record_error()
        assert observer.disabled
        assert observer.error_count == 3
