"""Manifest metrics observer — in-process counters + structured logging (Issue #1428).

Provides lightweight observability for context manifest resolution without
external dependencies. Follows the QueryObserver pattern from
observability_subsystem.py.

Features:
    - Per-resolution and per-source counters
    - Slow source detection with configurable threshold
    - Circuit breaker: auto-disables after repeated errors
    - Thread-safe counter mutations
    - Snapshot for health API consumption
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ManifestMetricsConfig:
    """Configuration for manifest metrics collection.

    Attributes:
        enabled: Whether metrics collection is active.
        slow_source_threshold_ms: Sources slower than this trigger a warning.
        max_listener_errors: Auto-disable after this many consecutive errors.
        log_source_names: Include source names in log messages.
    """

    enabled: bool = True
    slow_source_threshold_ms: float = 2000.0
    max_listener_errors: int = 10
    log_source_names: bool = True


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """Captured source execution event."""

    source_type: str
    source_name: str
    status: str
    elapsed_ms: float
    is_slow: bool
    timestamp: float = field(default_factory=time.time)


class ManifestMetricsObserver:
    """Collects metrics for manifest resolution and source execution.

    Thread-safe: all counter mutations are protected by a lock.
    Circuit breaker: auto-disables after max_listener_errors to prevent
    observability bugs from affecting the hot path.
    """

    def __init__(self, config: ManifestMetricsConfig | None = None) -> None:
        self._config = config or ManifestMetricsConfig()
        self._lock = threading.Lock()

        # Resolution-level counters
        self._total_resolutions: int = 0
        self._total_resolution_errors: int = 0
        self._active_resolutions: int = 0

        # Source-level counters
        self._total_source_executions: int = 0
        self._slow_sources: int = 0
        self._source_executions: dict[str, int] = {}
        self._source_status_counts: dict[str, dict[str, int]] = {}
        self._source_slow_counts: dict[str, int] = {}

        # Circuit breaker
        self._error_count: int = 0
        self._disabled: bool = False

    # -------------------------------------------------------------------
    # Resolution hooks
    # -------------------------------------------------------------------

    def on_resolution_start(self) -> None:
        """Called when a manifest resolution begins."""
        if not self._config.enabled or self._disabled:
            return
        try:
            with self._lock:
                self._active_resolutions += 1
        except Exception:
            self._record_error()

    def on_resolution_end(self, elapsed_ms: float, source_count: int, error: bool = False) -> None:
        """Called when a manifest resolution completes.

        Args:
            elapsed_ms: Total resolution time in milliseconds.
            source_count: Number of sources resolved.
            error: Whether the resolution ended with an error.
        """
        if not self._config.enabled or self._disabled:
            return
        try:
            with self._lock:
                self._active_resolutions = max(0, self._active_resolutions - 1)
                self._total_resolutions += 1
                if error:
                    self._total_resolution_errors += 1

            logger.debug(
                "Manifest resolution completed: %d sources in %.2fms (error=%s)",
                source_count,
                elapsed_ms,
                error,
            )
        except Exception:
            self._record_error()

    # -------------------------------------------------------------------
    # Source hooks
    # -------------------------------------------------------------------

    def on_source_complete(
        self,
        source_type: str,
        source_name: str,
        status: str,
        elapsed_ms: float,
    ) -> None:
        """Called after each source execution completes.

        Args:
            source_type: The source type (e.g., "file_glob", "memory_query").
            source_name: Human-readable source name.
            status: Execution status ("ok", "error", "timeout", etc.).
            elapsed_ms: Execution time in milliseconds.
        """
        if not self._config.enabled or self._disabled:
            return
        try:
            is_slow = elapsed_ms >= self._config.slow_source_threshold_ms

            with self._lock:
                self._total_source_executions += 1

                # Per-type execution count
                self._source_executions[source_type] = (
                    self._source_executions.get(source_type, 0) + 1
                )

                # Per-type status breakdown
                if source_type not in self._source_status_counts:
                    self._source_status_counts[source_type] = {}
                type_statuses = self._source_status_counts[source_type]
                type_statuses[status] = type_statuses.get(status, 0) + 1

                # Slow source tracking
                if is_slow:
                    self._slow_sources += 1
                    self._source_slow_counts[source_type] = (
                        self._source_slow_counts.get(source_type, 0) + 1
                    )

            # Log warnings for slow/error sources outside the lock
            if is_slow:
                name_part = f" ({source_name})" if self._config.log_source_names else ""
                logger.warning(
                    "Slow source: %s%s took %.2fms (threshold: %.0fms)",
                    source_type,
                    name_part,
                    elapsed_ms,
                    self._config.slow_source_threshold_ms,
                )

            if status in ("error", "timeout"):
                name_part = f" ({source_name})" if self._config.log_source_names else ""
                logger.warning(
                    "Source %s: %s%s in %.2fms",
                    status,
                    source_type,
                    name_part,
                    elapsed_ms,
                )

        except Exception:
            self._record_error()

    # -------------------------------------------------------------------
    # Read-only accessors
    # -------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a thread-safe copy of all metrics for health API.

        Returns:
            Dict with all current metric values.
        """
        with self._lock:
            return {
                "total_resolutions": self._total_resolutions,
                "total_resolution_errors": self._total_resolution_errors,
                "active_resolutions": self._active_resolutions,
                "total_source_executions": self._total_source_executions,
                "slow_sources": self._slow_sources,
                "disabled": self._disabled,
                "error_count": self._error_count,
                "by_source_type": {
                    stype: {
                        "executions": self._source_executions.get(stype, 0),
                        "statuses": dict(self._source_status_counts.get(stype, {})),
                        "slow": self._source_slow_counts.get(stype, 0),
                    }
                    for stype in self._source_executions
                },
            }

    def reset(self) -> None:
        """Reset all counters and re-enable if disabled. For testing."""
        with self._lock:
            self._total_resolutions = 0
            self._total_resolution_errors = 0
            self._active_resolutions = 0
            self._total_source_executions = 0
            self._slow_sources = 0
            self._source_executions.clear()
            self._source_status_counts.clear()
            self._source_slow_counts.clear()
            self._error_count = 0
            self._disabled = False

    # -------------------------------------------------------------------
    # Circuit breaker
    # -------------------------------------------------------------------

    def _record_error(self) -> None:
        """Record an internal observer error. Auto-disable after threshold."""
        with self._lock:
            self._error_count += 1
            if self._error_count >= self._config.max_listener_errors:
                self._disabled = True
                logger.error(
                    "ManifestMetricsObserver auto-disabled after %d errors",
                    self._error_count,
                )
