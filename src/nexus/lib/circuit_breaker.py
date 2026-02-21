"""Shared circuit breaker base — single ``CircuitState`` enum and base class.

Provides the canonical three-state machine (CLOSED → OPEN → HALF_OPEN)
used by all circuit breaker implementations in the codebase:

- ``nexus.lib.resiliency.AsyncCircuitBreaker`` (decorator-based)
- ``nexus.bricks.rebac.circuit_breaker.AsyncCircuitBreaker`` (DB resilience)
- ``nexus.proxy.circuit_breaker.AsyncCircuitBreaker`` (remote proxy)

Design:
    - Lock-free reads on ``current_state`` (hot path).
    - ``asyncio.Lock`` only on state transitions.
    - ``time.monotonic`` for timing (wall-clock independent).

Issue #2125.
"""

import asyncio
import logging
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerBase:
    """Base circuit breaker with three-state machine.

    Subclasses may override ``_record_failure`` / ``_record_success``
    for domain-specific behaviour (sliding windows, metrics, etc.)
    but the core state transitions are handled here.

    Args:
        failure_threshold: Failures before CLOSED → OPEN.
        success_threshold: Successes in HALF_OPEN before → CLOSED.
        reset_timeout: Seconds in OPEN before auto-transition to HALF_OPEN.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        success_threshold: int = 3,
        reset_timeout: float = 30.0,
    ) -> None:
        self._state: CircuitState = CircuitState.CLOSED
        self._opened_at: float | None = None
        self._failure_count: int = 0
        self._success_count: int = 0
        self._failure_threshold = failure_threshold
        self._success_threshold = success_threshold
        self._reset_timeout = reset_timeout
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Properties (lock-free reads)
    # ------------------------------------------------------------------

    @property
    def current_state(self) -> CircuitState:
        """Return effective state with lazy OPEN → HALF_OPEN check."""
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self._reset_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def success_count(self) -> int:
        return self._success_count

    # ------------------------------------------------------------------
    # State transitions (lock-protected)
    # ------------------------------------------------------------------

    async def _record_failure(self) -> None:
        """Record a failure; trip breaker if threshold reached."""
        async with self._lock:
            self._failure_count += 1
            effective = self._effective_state()

            if effective is CircuitState.CLOSED:
                if self._failure_count >= self._failure_threshold:
                    self._transition_to_open()

            elif effective is CircuitState.HALF_OPEN:
                self._transition_to_open()

    async def _record_success(self) -> None:
        """Record a success; close breaker if threshold reached in HALF_OPEN."""
        effective = self._effective_state()

        if effective is CircuitState.CLOSED:
            self._failure_count = 0
            return

        if effective is CircuitState.HALF_OPEN:
            async with self._lock:
                # Re-check under lock
                if self._effective_state() is not CircuitState.HALF_OPEN:
                    return
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    self._transition_to_closed()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_state(self) -> CircuitState:
        """Compute effective state (auto OPEN → HALF_OPEN after timeout)."""
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self._reset_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state

    def _transition_to_open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._success_count = 0

    def _transition_to_closed(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = None
