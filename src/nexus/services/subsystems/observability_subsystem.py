"""Observability Subsystem — SQLAlchemy event listeners for query monitoring.

Issue #1301: Query Observability — SQLAlchemy Listeners + OTel.

Provides:
- ``QueryEvent``: Frozen dataclass for captured query execution data.
- ``QueryObserver``: Attaches SQLAlchemy event listeners for slow query detection,
  structured logging, and connection pool metrics. Includes a circuit breaker
  that auto-disables after repeated errors to protect the hot path.
- ``ObservabilitySubsystem(Subsystem)``: Lifecycle wrapper with health_check/cleanup.

Constructor takes explicit deps — no ``self`` god-reference to NexusFS.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import event

from nexus.core.config import ObservabilityConfig
from nexus.services.subsystem import Subsystem

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryEvent:
    """Captured query execution event."""

    statement: str
    duration_ms: float
    is_slow: bool
    timestamp: float
    connection_id: str | None = None


class QueryObserver:
    """Attaches SQLAlchemy event listeners for query timing and pool metrics.

    Designed for the hot path: timing + threshold check always runs;
    structured logging only fires for slow queries.

    A circuit breaker auto-disables listeners after ``max_listener_errors``
    consecutive failures to prevent observability bugs from affecting queries.

    Thread safety: all counter mutations are protected by ``_lock``.
    """

    def __init__(self, config: ObservabilityConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._error_count = 0
        self._disabled = False
        self._total_queries = 0
        self._slow_queries = 0
        self._last_event: QueryEvent | None = None
        self._engines: list[Engine] = []

        # Pool counters
        self._pool_checkouts = 0
        self._pool_checkins = 0
        self._pool_connects = 0
        self._pool_invalidations = 0

    # -- Public properties ---------------------------------------------------

    @property
    def total_queries(self) -> int:
        return self._total_queries

    @property
    def slow_queries(self) -> int:
        return self._slow_queries

    @property
    def last_event(self) -> QueryEvent | None:
        return self._last_event

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def pool_checkouts(self) -> int:
        return self._pool_checkouts

    @property
    def pool_checkins(self) -> int:
        return self._pool_checkins

    @property
    def pool_connects(self) -> int:
        return self._pool_connects

    @property
    def pool_invalidations(self) -> int:
        return self._pool_invalidations

    @property
    def engines(self) -> list[Engine]:
        """Return a copy of instrumented engines."""
        return list(self._engines)

    # -- Engine instrumentation ----------------------------------------------

    def instrument_engine(self, engine: Engine) -> None:
        """Attach event listeners to a SQLAlchemy engine."""
        if self._config.enable_query_logging:
            event.listen(engine, "before_cursor_execute", self._before_cursor_execute)
            event.listen(engine, "after_cursor_execute", self._after_cursor_execute)

        if self._config.enable_pool_metrics:
            pool = engine.pool
            event.listen(pool, "checkout", self._on_pool_checkout)
            event.listen(pool, "checkin", self._on_pool_checkin)
            event.listen(pool, "connect", self._on_pool_connect)
            event.listen(pool, "invalidate", self._on_pool_invalidate)

        self._engines.append(engine)

    def remove_listeners(self, engine: Engine) -> None:
        """Remove all event listeners from an engine."""
        if self._config.enable_query_logging:
            event.remove(engine, "before_cursor_execute", self._before_cursor_execute)
            event.remove(engine, "after_cursor_execute", self._after_cursor_execute)

        if self._config.enable_pool_metrics:
            pool = engine.pool
            event.remove(pool, "checkout", self._on_pool_checkout)
            event.remove(pool, "checkin", self._on_pool_checkin)
            event.remove(pool, "connect", self._on_pool_connect)
            event.remove(pool, "invalidate", self._on_pool_invalidate)

    def clear_engines(self) -> None:
        """Clear the list of tracked engines."""
        self._engines.clear()

    def reset_counters(self) -> None:
        """Reset all counters and state."""
        with self._lock:
            self._total_queries = 0
            self._slow_queries = 0
            self._error_count = 0
            self._disabled = False
            self._last_event = None
            self._pool_checkouts = 0
            self._pool_checkins = 0
            self._pool_connects = 0
            self._pool_invalidations = 0

    # -- SQLAlchemy event handlers -------------------------------------------

    def _before_cursor_execute(
        self,
        _conn: Any,
        _cursor: Any,
        _statement: Any,
        _parameters: Any,
        context: Any,
        _executemany: Any,
    ) -> None:
        if self._disabled:
            return
        try:
            if context is not None:
                # Stash start time on execution context for duration calculation
                context._observability_start_ns = time.perf_counter_ns()
        except Exception:
            self._record_error()

    def _after_cursor_execute(
        self,
        _conn: Any,
        _cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        _executemany: Any,
    ) -> None:
        if self._disabled:
            return
        try:
            duration_ms = self._compute_duration(context)
            is_slow = duration_ms >= self._config.slow_query_threshold_ms
            truncated = self._truncate_statement(str(statement))

            evt = QueryEvent(
                statement=truncated,
                duration_ms=duration_ms,
                is_slow=is_slow,
                timestamp=time.time(),
            )

            with self._lock:
                self._total_queries += 1
                self._last_event = evt
                if is_slow:
                    self._slow_queries += 1

            if is_slow:
                extra: dict[str, Any] = {
                    "duration_ms": duration_ms,
                    "statement": truncated,
                }
                if self._config.log_query_parameters:
                    extra["parameters"] = parameters
                logger.warning(
                    "Slow query detected: %.2fms — %s",
                    duration_ms,
                    truncated,
                    extra=extra,
                )

        except Exception:
            self._record_error()

    def _compute_duration(self, context: Any) -> float:
        """Compute query duration in milliseconds from context start time."""
        start_ns = getattr(context, "_observability_start_ns", None)
        if start_ns is None:
            return 0.0
        elapsed: float = (time.perf_counter_ns() - start_ns) / 1_000_000
        return elapsed

    # -- Pool event handlers -------------------------------------------------

    def _on_pool_checkout(
        self, _dbapi_conn: Any, _connection_record: Any, _connection_proxy: Any
    ) -> None:
        with self._lock:
            self._pool_checkouts += 1

    def _on_pool_checkin(self, _dbapi_conn: Any, _connection_record: Any) -> None:
        with self._lock:
            self._pool_checkins += 1

    def _on_pool_connect(self, _dbapi_conn: Any, _connection_record: Any) -> None:
        with self._lock:
            self._pool_connects += 1

    def _on_pool_invalidate(
        self, _dbapi_conn: Any, _connection_record: Any, _exception: Any
    ) -> None:
        with self._lock:
            self._pool_invalidations += 1

    # -- Helpers -------------------------------------------------------------

    def _truncate_statement(self, statement: str) -> str:
        """Truncate SQL statement to configured max length."""
        max_len = self._config.max_query_length
        if len(statement) > max_len:
            return statement[:max_len] + "..."
        return statement

    def _record_error(self) -> None:
        """Increment error count; auto-disable if threshold exceeded."""
        with self._lock:
            self._error_count += 1
            if self._error_count >= self._config.max_listener_errors:
                self._disabled = True
                logger.error(
                    "ObservabilitySubsystem auto-disabled after %d listener errors",
                    self._error_count,
                )


class ObservabilitySubsystem(Subsystem):
    """Lifecycle wrapper for QueryObserver.

    Instruments SQLAlchemy engines with event listeners for slow query
    detection, structured logging, and pool metrics.

    Args:
        config: ObservabilityConfig with thresholds and toggles.
        engines: Initial engines to instrument (more can be added later).
    """

    def __init__(
        self,
        config: ObservabilityConfig,
        engines: Sequence[Engine] = (),
    ) -> None:
        self._config = config
        self._observer = QueryObserver(config=config)
        for engine in engines:
            self._observer.instrument_engine(engine)
        logger.info("[ObservabilitySubsystem] Initialized with %d engine(s)", len(engines))

    @property
    def observer(self) -> QueryObserver:
        """Access the underlying QueryObserver."""
        return self._observer

    def instrument_engine(self, engine: Engine) -> None:
        """Instrument an additional engine after construction."""
        self._observer.instrument_engine(engine)

    def health_check(self) -> dict[str, Any]:
        """Return health status for the observability subsystem."""
        status = "ok" if not self._observer.disabled else "degraded"
        return {
            "status": status,
            "subsystem": "observability",
            "query_observer_enabled": not self._observer.disabled,
            "error_count": self._observer.error_count,
            "total_queries": self._observer.total_queries,
            "slow_queries": self._observer.slow_queries,
            "pool_checkouts": self._observer.pool_checkouts,
            "pool_checkins": self._observer.pool_checkins,
            "pool_connects": self._observer.pool_connects,
            "pool_invalidations": self._observer.pool_invalidations,
        }

    def cleanup(self) -> None:
        """Remove event listeners and reset counters."""
        for engine in self._observer.engines:
            try:
                self._observer.remove_listeners(engine)
            except Exception:
                logger.debug("Failed to remove listeners from engine", exc_info=True)
        self._observer.clear_engines()
        self._observer.reset_counters()
