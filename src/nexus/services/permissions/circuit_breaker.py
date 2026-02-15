"""Circuit breaker for database resilience (Issue #726).

Implements the circuit breaker pattern to detect repeated DB failures,
fail fast during outages, and recover gracefully when the DB comes back.

States:
    CLOSED  — Normal operation. Failures are counted.
    OPEN    — DB unreachable. Requests fail immediately (no thread spawn).
    HALF_OPEN — Probe mode. A limited number of requests are allowed through
                to test recovery. Successes close the circuit; a failure reopens it.

Design decisions:
    - Lock-free reads on hot path (Decision 13A)
    - Sliding time window for failure counting (Decision 15A)
    - Check state BEFORE asyncio.to_thread() (Decision 14A)
    - Metrics as class attributes (Decision 16A)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from sqlalchemy.exc import InterfaceError, OperationalError

from nexus.core.exceptions import CircuitOpenError

T = TypeVar("T")

# Decision 7A: Explicit allowlist of infrastructure exceptions that trip the breaker.
INFRASTRUCTURE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OperationalError,  # sqlalchemy.exc — connection lost, query timeout
    InterfaceError,  # sqlalchemy.exc — driver-level failure
    TimeoutError,  # builtin — asyncio / socket timeout
    ConnectionError,  # builtin — TCP-level failure
    OSError,  # builtin — low-level I/O failure
)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Configuration for the circuit breaker.

    Attributes:
        failure_threshold: Number of failures within the window to trip the breaker.
        success_threshold: Consecutive successes in HALF_OPEN to close the circuit.
        reset_timeout: Seconds to wait in OPEN before transitioning to HALF_OPEN.
        failure_window: Sliding window in seconds for counting failures.
        excluded_exceptions: Exception types that do NOT trip the breaker
            (e.g., ValueError, PermissionError — business logic errors).
    """

    failure_threshold: int = 5
    success_threshold: int = 3
    reset_timeout: float = 30.0
    failure_window: float = 60.0
    excluded_exceptions: frozenset[type[BaseException]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.success_threshold < 1:
            raise ValueError("success_threshold must be >= 1")
        if self.reset_timeout <= 0:
            raise ValueError("reset_timeout must be > 0")
        if self.failure_window <= 0:
            raise ValueError("failure_window must be > 0")


class AsyncCircuitBreaker:
    """Async circuit breaker with lock-free reads and sliding failure window.

    Usage::

        cb = AsyncCircuitBreaker(name="rebac_db")
        result = await cb.call(asyncio.to_thread, some_sync_fn, arg1, arg2)
    """

    def __init__(
        self,
        name: str = "default",
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        self._name = name
        self._config = config or CircuitBreakerConfig()

        # State (lock-free read, lock on transitions)
        self._state: CircuitState = CircuitState.CLOSED
        self._lock = asyncio.Lock()

        # Sliding window of failure timestamps (Decision 15A)
        # maxlen caps memory at 2x threshold to bound growth during sustained failures.
        self._failure_timestamps: deque[float] = deque(
            maxlen=self._config.failure_threshold * 2
        )

        # Half-open success counter
        self._half_open_successes: int = 0

        # Timing
        self._last_failure_time: float | None = None
        self._last_state_change_time: float = time.monotonic()
        self._opened_at: float | None = None

        # Metrics (Decision 16A)
        self._total_failure_count: int = 0
        self._total_success_count: int = 0
        self._open_count: int = 0

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        """Current state with automatic OPEN → HALF_OPEN transition on timeout."""
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._config.reset_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        """Number of failures in the current sliding window."""
        cutoff = time.monotonic() - self._config.failure_window
        return sum(1 for t in self._failure_timestamps if t >= cutoff)

    @property
    def success_count(self) -> int:
        return self._total_success_count

    @property
    def open_count(self) -> int:
        return self._open_count

    @property
    def last_failure_time(self) -> float | None:
        return self._last_failure_time

    @property
    def last_state_change_time(self) -> float:
        return self._last_state_change_time

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def call(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute *fn* with circuit breaker protection.

        Args:
            fn: Callable to execute (e.g., ``asyncio.to_thread``).
            *args: Positional arguments forwarded to *fn*.
            **kwargs: Keyword arguments forwarded to *fn*.

        Returns:
            The return value of *fn*.

        Raises:
            CircuitOpenError: If the circuit is OPEN (fail fast).
        """
        current_state = self.state  # lock-free read

        if current_state == CircuitState.OPEN:
            raise CircuitOpenError(self._name)

        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:
            if self._is_excluded(exc):
                raise
            await self._on_failure()
            raise
        else:
            await self._on_success(current_state)
            return result

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_timestamps.clear()
        self._half_open_successes = 0
        self._opened_at = None
        self._last_failure_time = None
        self._last_state_change_time = time.monotonic()
        self._total_failure_count = 0
        self._total_success_count = 0
        self._open_count = 0

    def describe(self) -> str:
        """Human-readable description for debugging."""
        return (
            f"CircuitBreaker(name={self._name!r}, state={self.state.value}, "
            f"failures={self.failure_count}/{self._config.failure_threshold}, "
            f"open_count={self._open_count})"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_excluded(self, exc: BaseException) -> bool:
        """Return True if *exc* should NOT trip the breaker."""
        return not isinstance(exc, INFRASTRUCTURE_EXCEPTIONS)

    def _prune_window(self) -> None:
        """Remove timestamps outside the sliding window."""
        cutoff = time.monotonic() - self._config.failure_window
        while self._failure_timestamps and self._failure_timestamps[0] < cutoff:
            self._failure_timestamps.popleft()

    async def _on_failure(self) -> None:
        """Record a failure and potentially open the circuit."""
        now = time.monotonic()
        async with self._lock:
            self._total_failure_count += 1
            self._last_failure_time = now
            self._failure_timestamps.append(now)
            self._prune_window()

            # Resolve effective state (OPEN with expired timeout = HALF_OPEN)
            current = self._state
            if (
                current == CircuitState.OPEN
                and self._opened_at is not None
                and (now - self._opened_at) >= self._config.reset_timeout
            ):
                current = CircuitState.HALF_OPEN

            if current == CircuitState.HALF_OPEN:
                # Any failure in HALF_OPEN → reopen
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._open_count += 1
                self._half_open_successes = 0
                self._last_state_change_time = now

            elif current == CircuitState.CLOSED:
                if len(self._failure_timestamps) >= self._config.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = now
                    self._open_count += 1
                    self._last_state_change_time = now

    async def _on_success(self, state_at_call_time: CircuitState) -> None:
        """Record a success and potentially close the circuit."""
        self._total_success_count += 1

        # Fast path: CLOSED state — no lock needed
        if state_at_call_time == CircuitState.CLOSED:
            return

        # HALF_OPEN: count consecutive successes
        if state_at_call_time == CircuitState.HALF_OPEN:
            async with self._lock:
                # Re-check state under lock (may have changed)
                if self._state == CircuitState.OPEN and self._opened_at is not None:
                    elapsed = time.monotonic() - self._opened_at
                    if elapsed >= self._config.reset_timeout:
                        self._state = CircuitState.HALF_OPEN
                        self._last_state_change_time = time.monotonic()

                if self._state == CircuitState.HALF_OPEN:
                    self._half_open_successes += 1
                    if self._half_open_successes >= self._config.success_threshold:
                        self._state = CircuitState.CLOSED
                        self._failure_timestamps.clear()
                        self._half_open_successes = 0
                        self._opened_at = None
                        self._last_state_change_time = time.monotonic()
