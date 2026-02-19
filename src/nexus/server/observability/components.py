"""Observability component adapters wrapping existing setup/shutdown functions.

Issue #2072: Each adapter implements the LifecycleComponent protocol,
delegating to existing functions without duplicating logic.
"""

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.services.subsystems.observability_subsystem import ObservabilitySubsystem

logger = logging.getLogger(__name__)


class LoggingComponent:
    """Wraps configure_logging() / shutdown_logging() as a lifecycle component."""

    def __init__(self, env: str | None = None) -> None:
        self._env = env or os.environ.get("NEXUS_ENV", "dev")
        self._started = False

    @property
    def name(self) -> str:
        return "logging"

    async def start(self) -> None:
        from nexus.server.logging_config import configure_logging

        configure_logging(env=self._env)
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:  # noqa: ARG002
        if not self._started:
            return
        try:
            from nexus.server.logging_config import shutdown_logging

            shutdown_logging()
        except ImportError:
            pass
        self._started = False

    def is_healthy(self) -> bool:
        return self._started


class OTelTracingComponent:
    """Wraps setup_telemetry() / shutdown_telemetry() as a lifecycle component."""

    def __init__(self) -> None:
        self._started = False

    @property
    def name(self) -> str:
        return "otel-tracing"

    async def start(self) -> None:
        from nexus.server.telemetry import setup_telemetry

        setup_telemetry()
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:  # noqa: ARG002
        if not self._started:
            return
        try:
            from nexus.server.telemetry import shutdown_telemetry

            shutdown_telemetry()
        except ImportError:
            pass
        self._started = False

    def is_healthy(self) -> bool:
        return self._started


class SentryComponent:
    """Wraps setup_sentry() / shutdown_sentry() as a lifecycle component."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._started = False

    @property
    def name(self) -> str:
        return "sentry"

    async def start(self) -> None:
        from nexus.server.sentry import setup_sentry

        setup_sentry(**self._kwargs)
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:  # noqa: ARG002
        if not self._started:
            return
        try:
            from nexus.server.sentry import shutdown_sentry

            shutdown_sentry()
        except ImportError:
            pass
        self._started = False

    def is_healthy(self) -> bool:
        return self._started


class PyroscopeComponent:
    """Wraps setup_profiling() / shutdown_profiling() as a lifecycle component."""

    def __init__(self) -> None:
        self._started = False

    @property
    def name(self) -> str:
        return "pyroscope"

    async def start(self) -> None:
        from nexus.server.profiling import setup_profiling

        setup_profiling()
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:  # noqa: ARG002
        if not self._started:
            return
        try:
            from nexus.server.profiling import shutdown_profiling

            shutdown_profiling()
        except ImportError:
            pass
        self._started = False

    def is_healthy(self) -> bool:
        return self._started


class PrometheusComponent:
    """Wraps setup_prometheus() / shutdown_prometheus() as a lifecycle component."""

    def __init__(self) -> None:
        self._started = False

    @property
    def name(self) -> str:
        return "prometheus"

    async def start(self) -> None:
        from nexus.server.metrics import setup_prometheus

        setup_prometheus()
        self._started = True

    async def shutdown(self, timeout_ms: int = 5000) -> None:  # noqa: ARG002
        if not self._started:
            return
        try:
            from nexus.server.metrics import shutdown_prometheus

            shutdown_prometheus()
        except ImportError:
            pass
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
