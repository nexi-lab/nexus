"""Async circuit breaker with three-state machine.

States: CLOSED -> OPEN -> HALF_OPEN -> CLOSED (on success) or OPEN (on failure).
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class AsyncCircuitBreaker:
    """Async-safe circuit breaker for remote call protection.

    Uses ``asyncio.Lock`` for coroutine safety and ``time.monotonic``
    for timing so that wall-clock adjustments don't affect behaviour.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current state, auto-transitioning OPEN -> HALF_OPEN after timeout."""
        if (
            self._state is CircuitState.OPEN
            and time.monotonic() - self._opened_at >= self._recovery_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state is CircuitState.OPEN

    async def record_success(self) -> None:
        """Record a successful call — close the circuit if half-open."""
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN or (
                self._state is CircuitState.OPEN
                and time.monotonic() - self._opened_at >= self._recovery_timeout
            ):
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_calls = 0
            elif self._state is CircuitState.CLOSED:
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed call — open the circuit if threshold is reached."""
        async with self._lock:
            effective = self._effective_state()
            if effective is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._half_open_calls = 0
            elif effective is CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()

    async def allow_request(self) -> bool:
        """Return True if a request should be allowed through."""
        async with self._lock:
            effective = self._effective_state()
            if effective is CircuitState.CLOSED:
                return True
            if effective is CircuitState.HALF_OPEN:
                if self._half_open_calls < self._half_open_max_calls:
                    self._half_open_calls += 1
                    # Commit transition so state property stays consistent
                    self._state = CircuitState.HALF_OPEN
                    return True
                return False
            return False  # OPEN

    async def reset(self) -> None:
        """Force-reset to CLOSED state."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _effective_state(self) -> CircuitState:
        """Compute effective state (auto OPEN->HALF_OPEN)."""
        if (
            self._state is CircuitState.OPEN
            and time.monotonic() - self._opened_at >= self._recovery_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state
