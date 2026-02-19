"""Sandbox router metrics — thread-safe in-process counters (Issue #1317).

Provides lightweight observability for the SandboxRouter without
external dependencies. Follows the WriteBackMetrics pattern.

Features:
    - Per-tier selection counters (monty, docker, e2b)
    - Escalation counter
    - Thread-safe mutations via lock
    - Snapshot for health API
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class SandboxRouterMetrics:
    """Thread-safe routing metrics for sandbox provider selection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tier_selections: defaultdict[str, int] = defaultdict(int)
        self._escalation_count: int = 0
        self._escalations_by_path: defaultdict[str, int] = defaultdict(int)

    def record_selection(self, tier: str) -> None:
        """Record a tier selection decision."""
        with self._lock:
            self._tier_selections[tier] += 1

    def record_escalation(self, from_tier: str, to_tier: str) -> None:
        """Record an escalation event from one tier to another."""
        with self._lock:
            self._escalation_count += 1
            self._escalations_by_path[f"{from_tier}->{to_tier}"] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a thread-safe copy of all metrics."""
        with self._lock:
            return {
                "tier_selections": dict(self._tier_selections),
                "escalation_count": self._escalation_count,
                "escalations_by_path": dict(self._escalations_by_path),
            }

    def reset(self) -> None:
        """Reset all counters. For testing."""
        with self._lock:
            self._tier_selections.clear()
            self._escalation_count = 0
            self._escalations_by_path.clear()
