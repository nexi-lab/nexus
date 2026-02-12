"""Tests for ObservabilitySubsystem — query observability with SQLAlchemy listeners.

Issue #1301: Query Observability — SQLAlchemy Listeners + OTel.
"""

from __future__ import annotations

import logging
import time
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from nexus.core.config import ObservabilityConfig
from nexus.services.subsystem import Subsystem
from nexus.services.subsystems.observability_subsystem import (
    ObservabilitySubsystem,
    QueryEvent,
    QueryObserver,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> ObservabilityConfig:
    """Default observability config."""
    return ObservabilityConfig()


@pytest.fixture
def fast_config() -> ObservabilityConfig:
    """Config with very low threshold to trigger slow query detection."""
    return ObservabilityConfig(slow_query_threshold_ms=0.001)


@pytest.fixture
def disabled_config() -> ObservabilityConfig:
    """Config with logging disabled."""
    return ObservabilityConfig(enable_query_logging=False)


@pytest.fixture
def sqlite_engine():
    """Create a real SQLite in-memory engine for testing."""
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE test_tbl (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.commit()
    return engine


@pytest.fixture
def observer(config: ObservabilityConfig) -> QueryObserver:
    """QueryObserver with default config."""
    return QueryObserver(config=config)


@pytest.fixture
def subsystem(config: ObservabilityConfig, sqlite_engine) -> ObservabilitySubsystem:
    """ObservabilitySubsystem wired to a SQLite engine."""
    return ObservabilitySubsystem(config=config, engines=[sqlite_engine])


# ---------------------------------------------------------------------------
# ObservabilityConfig tests
# ---------------------------------------------------------------------------


class TestObservabilityConfig:
    """Configuration dataclass tests."""

    def test_default_config_values(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.slow_query_threshold_ms == 500.0
        assert cfg.enable_query_logging is True
        assert cfg.enable_pool_metrics is True
        assert cfg.log_query_parameters is False
        assert cfg.max_query_length == 1000
        assert cfg.max_listener_errors == 10

    def test_config_is_frozen(self) -> None:
        cfg = ObservabilityConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.slow_query_threshold_ms = 100.0  # type: ignore[misc]

    def test_custom_threshold(self) -> None:
        cfg = ObservabilityConfig(slow_query_threshold_ms=100.0, max_query_length=500)
        assert cfg.slow_query_threshold_ms == 100.0
        assert cfg.max_query_length == 500


# ---------------------------------------------------------------------------
# QueryEvent tests
# ---------------------------------------------------------------------------


class TestQueryEvent:
    """QueryEvent frozen dataclass tests."""

    def test_create_query_event(self) -> None:
        evt = QueryEvent(
            statement="SELECT 1",
            duration_ms=1.5,
            is_slow=False,
            timestamp=time.time(),
        )
        assert evt.statement == "SELECT 1"
        assert evt.duration_ms == 1.5
        assert evt.is_slow is False

    def test_query_event_is_frozen(self) -> None:
        evt = QueryEvent(statement="SELECT 1", duration_ms=1.0, is_slow=False, timestamp=0.0)
        with pytest.raises(FrozenInstanceError):
            evt.duration_ms = 99.0  # type: ignore[misc]

    def test_query_event_with_connection_id(self) -> None:
        evt = QueryEvent(
            statement="SELECT 1",
            duration_ms=0.5,
            is_slow=False,
            timestamp=0.0,
            connection_id="conn_123",
        )
        assert evt.connection_id == "conn_123"

    def test_query_event_default_connection_id_none(self) -> None:
        evt = QueryEvent(statement="SELECT 1", duration_ms=0.5, is_slow=False, timestamp=0.0)
        assert evt.connection_id is None


# ---------------------------------------------------------------------------
# Subsystem ABC compliance (standard 3 tests for every subsystem)
# ---------------------------------------------------------------------------


class TestObservabilitySubsystemCompliance:
    """Subsystem ABC contract tests."""

    def test_is_subsystem_instance(self, subsystem: ObservabilitySubsystem) -> None:
        assert isinstance(subsystem, Subsystem)

    def test_health_check_returns_dict_with_status(self, subsystem: ObservabilitySubsystem) -> None:
        result = subsystem.health_check()
        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] in ("ok", "degraded")

    def test_cleanup_callable_and_no_raise(self, subsystem: ObservabilitySubsystem) -> None:
        assert callable(subsystem.cleanup)
        subsystem.cleanup()


# ---------------------------------------------------------------------------
# Normal query timing (SQLite in-memory)
# ---------------------------------------------------------------------------


class TestQueryTiming:
    """Tests for query duration capture."""

    def test_captures_query_duration(self, sqlite_engine, config) -> None:
        observer = QueryObserver(config=config)
        observer.instrument_engine(sqlite_engine)

        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # Observer should have recorded at least one event
        assert observer.total_queries >= 1

    def test_fast_query_not_marked_slow(self, sqlite_engine) -> None:
        # Use high threshold so nothing is slow
        cfg = ObservabilityConfig(slow_query_threshold_ms=999_999.0)
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(sqlite_engine)

        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        assert observer.slow_queries == 0

    def test_slow_query_marked_slow_and_logged(self, sqlite_engine, caplog) -> None:
        # Use near-zero threshold so everything is "slow"
        cfg = ObservabilityConfig(slow_query_threshold_ms=0.0001)
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(sqlite_engine)

        with (
            caplog.at_level(
                logging.WARNING, logger="nexus.services.subsystems.observability_subsystem"
            ),
            sqlite_engine.connect() as conn,
        ):
            conn.execute(text("SELECT 1"))

        assert observer.slow_queries >= 1
        assert any("slow query" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Statement handling
# ---------------------------------------------------------------------------


class TestStatementHandling:
    """Tests for SQL statement truncation and parameter handling."""

    def test_long_statement_truncated(self, sqlite_engine) -> None:
        cfg = ObservabilityConfig(max_query_length=20)
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(sqlite_engine)

        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM test_tbl WHERE name = 'a very long string here'"))

        assert observer.last_event is not None
        assert len(observer.last_event.statement) <= 20 + 3  # +3 for "..."

    def test_query_parameters_not_logged_by_default(self, sqlite_engine, caplog) -> None:
        cfg = ObservabilityConfig(
            slow_query_threshold_ms=0.0001,  # force slow log
            log_query_parameters=False,
        )
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(sqlite_engine)

        with (
            caplog.at_level(
                logging.WARNING, logger="nexus.services.subsystems.observability_subsystem"
            ),
            sqlite_engine.connect() as conn,
        ):
            conn.execute(text("SELECT * FROM test_tbl WHERE id = :id"), {"id": 42})

        # Parameters should NOT appear in log messages
        log_text = " ".join(r.message for r in caplog.records)
        assert "42" not in log_text


# ---------------------------------------------------------------------------
# Error handling + circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """Tests for listener error handling and auto-disable."""

    def test_listener_error_does_not_break_query(self, sqlite_engine) -> None:
        observer = QueryObserver(config=ObservabilityConfig())
        observer.instrument_engine(sqlite_engine)

        # Inject a fault into the after_cursor_execute path
        with (
            patch.object(observer, "_compute_duration", side_effect=RuntimeError("boom")),
            sqlite_engine.connect() as conn,
        ):
            result = conn.execute(text("SELECT 1"))
            row = result.fetchone()
            assert row is not None
            assert row[0] == 1

        assert observer.error_count >= 1

    def test_auto_disable_after_error_threshold(self, sqlite_engine) -> None:
        cfg = ObservabilityConfig(max_listener_errors=3)
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(sqlite_engine)

        with (
            patch.object(observer, "_compute_duration", side_effect=RuntimeError("boom")),
            sqlite_engine.connect() as conn,
        ):
            for _ in range(5):
                conn.execute(text("SELECT 1"))

        assert observer.disabled is True
        assert observer.error_count >= 3

    def test_health_check_reports_degraded_after_disable(self, sqlite_engine) -> None:
        cfg = ObservabilityConfig(max_listener_errors=1)
        sub = ObservabilitySubsystem(config=cfg, engines=[sqlite_engine])

        with (
            patch.object(sub._observer, "_compute_duration", side_effect=RuntimeError("boom")),
            sqlite_engine.connect() as conn,
        ):
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 1"))

        health = sub.health_check()
        assert health["status"] == "degraded"
        assert health["query_observer_enabled"] is False

    def test_queries_still_execute_after_disable(self, sqlite_engine) -> None:
        cfg = ObservabilityConfig(max_listener_errors=1)
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(sqlite_engine)

        # Force disable
        with (
            patch.object(observer, "_compute_duration", side_effect=RuntimeError("boom")),
            sqlite_engine.connect() as conn,
        ):
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 1"))

        assert observer.disabled is True

        # Queries should still work fine after observer is disabled
        with sqlite_engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Pool metrics
# ---------------------------------------------------------------------------


class TestPoolMetrics:
    """Tests for connection pool counter tracking."""

    def test_pool_checkout_counter_increments(self) -> None:
        # Use a pool-backed engine (NullPool won't fire pool events)
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        cfg = ObservabilityConfig(enable_pool_metrics=True)
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(engine)

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        assert observer.pool_checkouts >= 1

    def test_pool_counters_in_health_check(self) -> None:
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        sub = ObservabilitySubsystem(
            config=ObservabilityConfig(enable_pool_metrics=True),
            engines=[engine],
        )

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        health = sub.health_check()
        assert "pool_checkouts" in health
        assert health["pool_checkouts"] >= 1
        assert "pool_checkins" in health


# ---------------------------------------------------------------------------
# Disabled config
# ---------------------------------------------------------------------------


class TestDisabledConfig:
    """Tests when query logging is disabled."""

    def test_disabled_config_no_listeners_attached(self, sqlite_engine) -> None:
        cfg = ObservabilityConfig(enable_query_logging=False, enable_pool_metrics=False)
        observer = QueryObserver(config=cfg)
        observer.instrument_engine(sqlite_engine)

        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # No queries tracked when disabled
        assert observer.total_queries == 0


# ---------------------------------------------------------------------------
# Multi-engine
# ---------------------------------------------------------------------------


class TestMultiEngine:
    """Tests for instrumenting multiple engines."""

    def test_instrument_multiple_engines(self) -> None:
        engine1 = create_engine("sqlite:///:memory:")
        engine2 = create_engine("sqlite:///:memory:")

        sub = ObservabilitySubsystem(
            config=ObservabilityConfig(),
            engines=[engine1, engine2],
        )

        with engine1.connect() as conn:
            conn.execute(text("SELECT 1"))
        with engine2.connect() as conn:
            conn.execute(text("SELECT 1"))

        assert sub._observer.total_queries >= 2


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for subsystem cleanup."""

    def test_cleanup_removes_listeners(self, sqlite_engine) -> None:
        sub = ObservabilitySubsystem(
            config=ObservabilityConfig(),
            engines=[sqlite_engine],
        )

        # Verify listeners are attached
        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        assert sub._observer.total_queries >= 1

        # Cleanup
        sub.cleanup()

        # Counters reset
        assert sub._observer.total_queries == 0

        # After cleanup, new queries should NOT be tracked
        with sqlite_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        assert sub._observer.total_queries == 0
