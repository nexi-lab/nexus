"""Write-back metrics — thread-safe in-process counters (Issue #1129).

Provides lightweight observability for the WriteBackService without
external dependencies. Follows the ManifestMetricsObserver pattern.

Features:
    - Global and per-backend push/failure/conflict counters
    - Thread-safe counter mutations via lock
    - Snapshot for health API / push endpoint response
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

_BACKEND_DEFAULTS = {"pushed": 0, "failed": 0, "conflicts": 0}


class WriteBackMetrics:
    """Thread-safe in-process counters for write-back observability."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changes_pushed: int = 0
        self._changes_failed: int = 0
        self._conflicts_detected: int = 0
        self._conflicts_auto_resolved: int = 0
        self._per_backend: defaultdict[str, dict[str, int]] = defaultdict(
            lambda: dict(_BACKEND_DEFAULTS)
        )

    def record_push(self, backend_name: str) -> None:
        """Record a successful write-back push."""
        with self._lock:
            self._changes_pushed += 1
            self._per_backend[backend_name]["pushed"] += 1

    def record_failure(self, backend_name: str) -> None:
        """Record a failed write-back attempt."""
        with self._lock:
            self._changes_failed += 1
            self._per_backend[backend_name]["failed"] += 1

    def record_conflict(self, backend_name: str, auto_resolved: bool = True) -> None:
        """Record a conflict detection (and optional auto-resolution)."""
        with self._lock:
            self._conflicts_detected += 1
            if auto_resolved:
                self._conflicts_auto_resolved += 1
            self._per_backend[backend_name]["conflicts"] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a thread-safe copy of all metrics."""
        with self._lock:
            return {
                "changes_pushed": self._changes_pushed,
                "changes_failed": self._changes_failed,
                "conflicts_detected": self._conflicts_detected,
                "conflicts_auto_resolved": self._conflicts_auto_resolved,
                "per_backend": {
                    name: dict(counts)
                    for name, counts in self._per_backend.items()
                },
            }

    def reset(self) -> None:
        """Reset all counters. For testing."""
        with self._lock:
            self._changes_pushed = 0
            self._changes_failed = 0
            self._conflicts_detected = 0
            self._conflicts_auto_resolved = 0
            self._per_backend.clear()
