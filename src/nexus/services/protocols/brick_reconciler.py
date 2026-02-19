"""Brick reconciler protocol, config, and backoff primitives (Issue #2059).

Defines the contract and data models for the self-healing brick reconciler —
a system service that detects failed/degraded bricks and automatically
attempts recovery with exponential backoff and jitter.

Architecture tier: System Service (alongside BrickLifecycleManager).

Backoff strategy: Decorrelated jitter per AWS recommendation:
    delay = min(max_delay, random.uniform(base_delay, last_delay * 3))
This provides optimal load distribution without thundering herd effects.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §2.4 (System Services)
    - AWS Builders Library: Timeouts, Retries, and Backoff with Jitter
    - Issue #2059: Self-healing brick reconciler
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrickReconcilerConfig:
    """Immutable configuration for the brick reconciler.

    Attributes:
        health_check_interval: Seconds between periodic health polls.
        base_delay: Initial backoff delay in seconds.
        max_delay: Maximum backoff delay cap in seconds.
        max_attempts: Dead-letter threshold — stop retrying after this many.
        health_check_timeout: Per-brick timeout for health_check() calls.
    """

    health_check_interval: float = 30.0
    base_delay: float = 1.0
    max_delay: float = 600.0  # 10 minutes
    max_attempts: int = 10
    health_check_timeout: float = 5.0


# ---------------------------------------------------------------------------
# Backoff state (immutable — new object per update)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackoffState:
    """Per-brick backoff tracking.  Immutable — each update returns a new instance.

    Attributes:
        attempt: Number of recovery attempts so far (0 = first attempt).
        last_delay: The delay used for the most recent retry.
        next_retry_at: Monotonic timestamp when the next retry is allowed.
    """

    attempt: int = 0
    last_delay: float = 0.0
    next_retry_at: float = 0.0


def compute_next_backoff(
    state: BackoffState,
    config: BrickReconcilerConfig,
    *,
    _now: float | None = None,
) -> BackoffState:
    """Compute the next backoff state using decorrelated jitter.

    Algorithm (AWS recommended):
        delay = min(max_delay, random.uniform(base_delay, last_delay * 3))

    For the first attempt (last_delay == 0), uses base_delay as the floor.

    Args:
        state: Current backoff state (immutable).
        config: Reconciler configuration with delay bounds.
        _now: Override for monotonic time (testing only).

    Returns:
        New BackoffState with incremented attempt and computed next_retry_at.
    """
    now = _now if _now is not None else time.monotonic()

    prev = state.last_delay if state.last_delay > 0 else config.base_delay
    delay = min(config.max_delay, random.uniform(config.base_delay, prev * 3))

    return BackoffState(
        attempt=state.attempt + 1,
        last_delay=delay,
        next_retry_at=now + delay,
    )


def reset_backoff() -> BackoffState:
    """Return a fresh zero-state backoff (used after successful recovery)."""
    return BackoffState()


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class BrickReconcilerProtocol(Protocol):
    """Contract for the brick reconciler system service.

    The reconciler runs as a background task, periodically health-checking
    active bricks and automatically recovering failed ones.
    """

    async def start(self) -> None:
        """Start the reconciler background loops."""
        ...

    async def stop(self) -> None:
        """Stop the reconciler and cancel background tasks."""
        ...

    def enqueue(self, brick_name: str) -> None:
        """Manually enqueue a brick for reconciliation."""
        ...
