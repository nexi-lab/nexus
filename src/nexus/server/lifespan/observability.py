"""Observability startup/shutdown via ObservabilityRegistry.

Issue #2072: Consolidate observability init/shutdown into unified registry.
Replaces 6 inline _startup_* / 3 shutdown_* calls with a single registry.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from nexus.server.observability.registry import ObservabilityRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def create_registry(*, write_observer: Any = None) -> ObservabilityRegistry:
    """Create and populate the observability registry.

    Registration order = startup order (dependencies first).

    Args:
        write_observer: Optional WriteBuffer instance for WriteBufferComponent.
    """
    from nexus.server.observability.components import FunctionPairComponent, WriteBufferComponent

    registry = ObservabilityRegistry()

    # Logging — lazy import start/stop functions
    env = os.environ.get("NEXUS_ENV", "dev")

    def _start_logging() -> None:
        from nexus.server.logging_config import configure_logging

        configure_logging(env=env)

    def _stop_logging() -> None:
        from nexus.server.logging_config import shutdown_logging

        shutdown_logging()

    registry.register(
        "logging",
        FunctionPairComponent("logging", start_fn=_start_logging, stop_fn=_stop_logging),
        required=False,
    )

    # OTel Tracing
    def _start_otel() -> None:
        from nexus.server.telemetry import setup_telemetry

        setup_telemetry()

    def _stop_otel() -> None:
        from nexus.server.telemetry import shutdown_telemetry

        shutdown_telemetry()

    registry.register(
        "otel-tracing",
        FunctionPairComponent("otel-tracing", start_fn=_start_otel, stop_fn=_stop_otel),
        required=False,
    )

    # Sentry
    def _start_sentry() -> None:
        from nexus.server.sentry import setup_sentry

        setup_sentry()

    def _stop_sentry() -> None:
        from nexus.server.sentry import shutdown_sentry

        shutdown_sentry()

    registry.register(
        "sentry",
        FunctionPairComponent("sentry", start_fn=_start_sentry, stop_fn=_stop_sentry),
        required=False,
    )

    # Pyroscope
    def _start_pyroscope() -> None:
        from nexus.server.profiling import setup_profiling

        setup_profiling()

    def _stop_pyroscope() -> None:
        from nexus.server.profiling import shutdown_profiling

        shutdown_profiling()

    registry.register(
        "pyroscope",
        FunctionPairComponent("pyroscope", start_fn=_start_pyroscope, stop_fn=_stop_pyroscope),
        required=False,
    )

    # Prometheus
    def _start_prometheus() -> None:
        from nexus.server.metrics import setup_prometheus

        setup_prometheus()

    def _stop_prometheus() -> None:
        from nexus.server.metrics import shutdown_prometheus

        shutdown_prometheus()

    registry.register(
        "prometheus",
        FunctionPairComponent("prometheus", start_fn=_start_prometheus, stop_fn=_stop_prometheus),
        required=False,
    )

    # WriteBuffer (Issue #1370) — managed shutdown, started by factory
    if write_observer is not None:
        registry.register("write-buffer", WriteBufferComponent(write_observer), required=False)

    # QueryObserver registered later after factory creates the subsystem

    return registry


async def startup_observability(app: FastAPI) -> None:
    """Initialize all observability subsystems via the registry."""
    write_observer = app.state.write_observer
    registry = create_registry(write_observer=write_observer)
    statuses = await registry.start_all()
    app.state.observability_registry = registry

    # Log startup summary
    started = [s.name for s in statuses if s.started]
    failed = [s.name for s in statuses if not s.started]
    if started:
        logger.info("Observability components started: %s", ", ".join(started))
    if failed:
        logger.info("Observability components skipped: %s", ", ".join(failed))

    logger.info("Starting FastAPI Nexus server...")
    _startup_thread_pool(app)


async def shutdown_observability(app: FastAPI) -> None:
    """Shutdown all observability components via the registry."""
    registry = app.state.observability_registry
    if registry:
        await registry.shutdown_all()
        app.state.observability_registry = None


def _startup_thread_pool(app: FastAPI) -> None:
    """Configure thread pool size (Issue #932)."""
    from anyio import to_thread

    limiter = to_thread.current_default_thread_limiter()
    limiter.total_tokens = app.state.thread_pool_size
    logger.info("Thread pool size set to %d", limiter.total_tokens)
