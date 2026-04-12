"""Observability component adapters wrapping existing setup/shutdown functions.

Issue #2072: Each adapter implements the LifecycleComponent protocol,
delegating to existing functions without duplicating logic.

FunctionPairComponent replaces 5 near-identical adapter classes with a
single generic class parameterized by start/shutdown callables.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.server.observability.observability_subsystem import ObservabilitySubsystem

logger = logging.getLogger(__name__)


class FunctionPairComponent:
    """Generic lifecycle component wrapping a start/shutdown function pair.

    Replaces LoggingComponent, OTelTracingComponent, SentryComponent,
    PyroscopeComponent, and PrometheusComponent with a single DRY class.

    Args:
        component_name: Human-readable name for logging.
        start_fn: Callable invoked during ``start()``.
        stop_fn: Optional callable invoked during ``shutdown()``.
        start_kwargs: Keyword arguments forwarded to ``start_fn``.
    """

    def __init__(
        self,
        component_name: str,
        *,
        start_fn: Callable[..., Any],
        stop_fn: Callable[[], Any] | None = None,
        start_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._name = component_name
        self._start_fn = start_fn
        self._stop_fn = stop_fn
        self._start_kwargs = start_kwargs or {}
        self._started = False

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self._start_fn(**self._start_kwargs)
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:  # noqa: ARG002
        if not self._started:
            return
        if self._stop_fn is not None:
            try:
                self._stop_fn()
            except Exception:
                logger.warning("Error in %s shutdown", self._name, exc_info=True)
        self._started = False

    def is_healthy(self) -> bool:
        return self._started


class QueryObserverComponent:
    """Wraps ObservabilitySubsystem as a lifecycle component."""

    def __init__(self, subsystem: "ObservabilitySubsystem") -> None:
        self._subsystem = subsystem
        self._started = False

    @property
    def name(self) -> str:
        return "query-observer"

    async def start(self) -> None:
        # ObservabilitySubsystem is already initialized with engines by the factory.
        # start() is a no-op since instrumentation happens at construction time.
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:  # noqa: ARG002
        if not self._started:
            return
        self._subsystem.cleanup()
        self._started = False

    def is_healthy(self) -> bool:
        if not self._started:
            return False
        health = self._subsystem.health_check()
        return health.get("status") == "ok"


class WriteBufferComponent:
    """Lifecycle component for write observer shutdown.

    The write observer is started in server lifespan (not by this component).
    This component manages its graceful shutdown during server teardown.

    Issue #809: RecordStoreWriteObserver (OBSERVE-phase) replaces WriteBuffer.
    Shutdown is now handled by _shutdown_pipe_consumers in services.py.
    This component is kept for observability registry health reporting.
    """

    def __init__(self, write_observer: Any) -> None:
        self._wo = write_observer
        self._started = False

    @property
    def name(self) -> str:
        return "write-buffer"

    async def start(self) -> None:
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:
        _ = timeout_ms  # Shutdown handled by _shutdown_pipe_consumers (Issue #809)
        self._started = False

    def is_healthy(self) -> bool:
        return self._started
