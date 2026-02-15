"""Declarative resiliency layer — circuit breakers, retries, and timeouts.

Provides a unified resiliency framework configured via YAML named policies
and applied via the ``@with_resiliency`` decorator, separating resiliency
policy from business logic.

Composition order (outer → inner): CircuitBreaker → Retry → Timeout

Usage::

    from nexus.core.resiliency import with_resiliency

    @with_resiliency(target="gcs")
    async def upload_file(data: bytes) -> str:
        ...

Issue #1366.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# 1A: Infrastructure exception whitelist
# ---------------------------------------------------------------------------

INFRA_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
)

_infra_exceptions_with_httpx: tuple[type[BaseException], ...] | None = None


def _get_infra_exceptions() -> tuple[type[BaseException], ...]:
    """Return infrastructure exceptions, lazily extending with httpx types."""
    global _infra_exceptions_with_httpx
    if _infra_exceptions_with_httpx is not None:
        return _infra_exceptions_with_httpx

    extras: list[type[BaseException]] = []
    try:
        import httpx

        extras.append(httpx.TransportError)
        extras.append(httpx.TimeoutException)
    except ImportError:
        pass

    _infra_exceptions_with_httpx = INFRA_EXCEPTIONS + tuple(extras)
    return _infra_exceptions_with_httpx


# ---------------------------------------------------------------------------
# 1B: Frozen dataclass config models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeoutPolicy:
    """Timeout policy — wraps the call with ``asyncio.timeout``."""

    seconds: float = 5.0


@dataclass(frozen=True)
class RetryPolicy:
    """Retry policy — exponential backoff with full jitter via tenacity."""

    max_retries: int = 3
    max_interval: float = 10.0
    multiplier: float = 2.0
    min_wait: float = 1.0


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    """Circuit breaker policy — trip after *failure_threshold* infra errors."""

    failure_threshold: int = 5
    success_threshold: int = 3
    timeout: float = 30.0  # seconds in OPEN before attempting HALF_OPEN


@dataclass(frozen=True)
class TargetBinding:
    """Maps a named target to named policies."""

    timeout: str = "default"
    retry: str = "default"
    circuit_breaker: str = "default"


@dataclass(frozen=True)
class ResiliencyConfig:
    """Top-level resiliency configuration (from YAML)."""

    timeouts: dict[str, TimeoutPolicy] = field(default_factory=lambda: {"default": TimeoutPolicy()})
    retries: dict[str, RetryPolicy] = field(default_factory=lambda: {"default": RetryPolicy()})
    circuit_breakers: dict[str, CircuitBreakerPolicy] = field(
        default_factory=lambda: {"default": CircuitBreakerPolicy()}
    )
    targets: dict[str, TargetBinding] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1C: CircuitBreakerOpenError
# ---------------------------------------------------------------------------


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected because the circuit breaker is open."""

    def __init__(self, name: str, state: CircuitState) -> None:
        self.name = name
        self.state = state
        super().__init__(f"Circuit breaker '{name}' is {state.value}")


# ---------------------------------------------------------------------------
# 1D: AsyncCircuitBreaker state machine
# ---------------------------------------------------------------------------


class AsyncCircuitBreaker:
    """Async context-manager circuit breaker with 3 states.

    Composition: Used as the outermost wrapper so that when the circuit
    is open, calls are rejected immediately without retrying or waiting
    for a timeout.

    **Thread Safety**: This class is NOT thread-safe. Each instance must
    be used within a single asyncio event loop. Do not share instances
    across threads or multiple event loops.
    """

    def __init__(self, name: str, policy: CircuitBreakerPolicy) -> None:
        self._name = name
        self._policy = policy
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._opened_at: float | None = None
        self._half_open_lock = asyncio.Lock()
        self._infra_exc = _get_infra_exceptions()

    # -- properties ----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def current_state(self) -> CircuitState:
        """Return fresh state, performing lazy timestamp check for OPEN → HALF_OPEN."""
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._policy.timeout:
                return CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def last_failure_time(self) -> float | None:
        return self._last_failure_time

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> AsyncCircuitBreaker:
        state = self.current_state

        if state is CircuitState.CLOSED:
            return self

        if state is CircuitState.OPEN:
            raise CircuitBreakerOpenError(self._name, state)

        # HALF_OPEN: allow exactly one probe
        if state is CircuitState.HALF_OPEN:
            acquired = await self._try_acquire_lock()
            if not acquired:
                raise CircuitBreakerOpenError(self._name, CircuitState.OPEN)
            self._state = CircuitState.HALF_OPEN
            return self

        return self  # pragma: no cover

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: Any,
    ) -> bool:
        if exc_type is not None and issubclass(exc_type, self._infra_exc):
            self._record_failure()
        elif exc_type is None:
            self._record_success()
        # Non-infra exceptions pass through without affecting CB state
        return False  # never suppress exceptions

    # -- internal helpers ----------------------------------------------------

    async def _try_acquire_lock(self) -> bool:
        """Non-blocking lock acquire for half-open single-probe.

        In asyncio's single-threaded model, no coroutine can interleave
        between the ``locked()`` check and the ``acquire()`` call since
        there is no suspension point between them.
        """
        if self._half_open_lock.locked():
            return False
        await self._half_open_lock.acquire()
        return True

    def _record_success(self) -> None:
        if self._state is CircuitState.CLOSED:
            self._failure_count = 0
            return

        if self._state is CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._policy.success_threshold:
                self._transition_to_closed()
                self._release_half_open_lock()

    def _record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state is CircuitState.CLOSED:
            if self._failure_count >= self._policy.failure_threshold:
                self._transition_to_open()

        elif self._state is CircuitState.HALF_OPEN:
            self._transition_to_open()
            self._release_half_open_lock()

    def _transition_to_open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._success_count = 0
        logger.warning(
            "Circuit breaker '%s' OPEN (failures=%d)",
            self._name,
            self._failure_count,
        )

    def _transition_to_closed(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = None
        logger.info("Circuit breaker '%s' CLOSED (recovered)", self._name)

    def _release_half_open_lock(self) -> None:
        if self._half_open_lock.locked():
            with contextlib.suppress(RuntimeError):
                self._half_open_lock.release()


# ---------------------------------------------------------------------------
# 1E: ResiliencyManager
# ---------------------------------------------------------------------------


class ResiliencyManager:
    """Singleton-style manager that owns circuit breaker instances and config."""

    def __init__(self, config: ResiliencyConfig) -> None:
        self._config = config
        self._breakers: dict[str, AsyncCircuitBreaker] = {}

    @property
    def config(self) -> ResiliencyConfig:
        return self._config

    def get_breaker(self, name: str) -> AsyncCircuitBreaker:
        """Get or create a circuit breaker by policy name (idempotent)."""
        if name in self._breakers:
            return self._breakers[name]

        policy = self._config.circuit_breakers.get(name)
        if policy is None:
            logger.warning("Unknown circuit breaker policy '%s', using default", name)
            policy = self._config.circuit_breakers.get("default", CircuitBreakerPolicy())

        # Double-check to prevent duplicate creation under concurrency
        if name not in self._breakers:
            self._breakers[name] = AsyncCircuitBreaker(name=name, policy=policy)
        return self._breakers[name]

    def resolve_target(self, target: str) -> TargetBinding:
        """Look up a target binding by name."""
        binding = self._config.targets.get(target)
        if binding is None:
            logger.warning("Unknown resiliency target '%s', using defaults", target)
            return TargetBinding()
        return binding

    def health_check(self) -> dict[str, Any]:
        """Return health status for all active circuit breakers."""
        breakers_health: dict[str, Any] = {}
        has_degraded = False

        for name, breaker in self._breakers.items():
            state = breaker.current_state
            breakers_health[name] = {
                "state": state.value,
                "failure_count": breaker.failure_count,
                "last_failure_time": breaker.last_failure_time,
            }
            if state is not CircuitState.CLOSED:
                has_degraded = True

        return {
            "status": "degraded" if has_degraded else "ok",
            "circuit_breakers": breakers_health,
        }


# ---------------------------------------------------------------------------
# 1F: with_resiliency decorator
# ---------------------------------------------------------------------------

_default_manager: ResiliencyManager | None = None


def set_default_manager(mgr: ResiliencyManager) -> None:
    """Set the module-level default ResiliencyManager."""
    global _default_manager
    _default_manager = mgr


def get_default_manager() -> ResiliencyManager | None:
    """Return the module-level default ResiliencyManager (or None)."""
    return _default_manager


def with_resiliency(
    target: str | None = None,
    *,
    timeout: str | float | None = None,
    retry: str | None = None,
    circuit_breaker: str | None = None,
    manager: ResiliencyManager | None = None,
) -> Callable[[F], F]:
    """Declarative resiliency decorator.

    Composition order (outer → inner): CB → Retry → Timeout.

    If no manager is available (not passed, no default set), the decorator
    is a no-op passthrough — this enables graceful operation without config.

    Args:
        target: Named target binding from config (resolves to policy names).
        timeout: Named timeout policy or explicit seconds.
        retry: Named retry policy.
        circuit_breaker: Named circuit breaker policy.
        manager: Explicit ResiliencyManager (falls back to module default).
    """

    def decorator(func: F) -> F:
        mgr = manager or _default_manager
        if mgr is None:
            return func  # no-op passthrough

        # Resolve policies from target or explicit params
        if target is not None:
            binding = mgr.resolve_target(target)
            timeout_name = binding.timeout
            retry_name = binding.retry
            cb_name = binding.circuit_breaker
        else:
            timeout_name = timeout if isinstance(timeout, str) else "default"
            retry_name = retry or "default"
            cb_name = circuit_breaker or "default"

        # Look up timeout policy
        if isinstance(timeout, (int, float)):
            timeout_seconds = float(timeout)
        else:
            tp = mgr.config.timeouts.get(
                timeout_name, mgr.config.timeouts.get("default", TimeoutPolicy())
            )
            timeout_seconds = tp.seconds

        # Look up retry policy
        rp = mgr.config.retries.get(retry_name, mgr.config.retries.get("default", RetryPolicy()))

        # Get circuit breaker
        cb = mgr.get_breaker(cb_name)

        # Build tenacity retry kwargs
        from tenacity import retry as tenacity_retry
        from tenacity import (
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
            wait_random,
        )

        retry_kwargs: dict[str, Any] = {
            "stop": stop_after_attempt(rp.max_retries + 1),
            "wait": wait_exponential(
                multiplier=rp.multiplier,
                min=rp.min_wait,
                max=rp.max_interval,
            )
            + wait_random(0, 1),
            "retry": retry_if_exception_type(_get_infra_exceptions()),
            "reraise": True,
        }

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with cb:
                # Retry wraps the timeout-guarded call
                @tenacity_retry(**retry_kwargs)
                async def _inner() -> Any:
                    if timeout_seconds > 0:
                        async with asyncio.timeout(timeout_seconds):
                            return await func(*args, **kwargs)
                    else:
                        return await func(*args, **kwargs)

                return await _inner()

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# 1G: Duration parser helper
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE)


def parse_duration(value: str | int | float) -> float:
    """Parse ``'5s'``, ``'60s'``, ``'10m'``, ``'1h'`` or numeric seconds.

    Returns:
        Duration in seconds as a float.

    Raises:
        ValueError: If the format is unrecognised.
    """
    if isinstance(value, (int, float)):
        return float(value)

    m = _DURATION_RE.match(value)
    if m is None:
        raise ValueError(f"Cannot parse duration: {value!r}")

    num = float(m.group(1))
    unit = m.group(2).lower()

    if unit == "m":
        return num * 60.0
    if unit == "h":
        return num * 3600.0
    return num  # seconds or bare number
