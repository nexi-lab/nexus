"""Observability startup/shutdown via ObservabilityRegistry.

Issue #2072: Consolidate observability init/shutdown into unified registry.
Replaces 6 inline _startup_* / 3 shutdown_* calls with a single registry.
"""

import logging
import os
from typing import TYPE_CHECKING

from nexus.server.observability.registry import ObservabilityRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_registry: ObservabilityRegistry | None = None


def create_registry() -> ObservabilityRegistry:
    """Create and populate the observability registry.

    Registration order = startup order (dependencies first).
    """
    from nexus.server.observability.components import (
        LoggingComponent,
        OTelTracingComponent,
        PrometheusComponent,
        PyroscopeComponent,
        SentryComponent,
    )

    registry = ObservabilityRegistry()

    env = os.environ.get("NEXUS_ENV", "dev")
    registry.register("logging", LoggingComponent(env=env), required=False)
    registry.register("otel-tracing", OTelTracingComponent(), required=False)
    registry.register("sentry", SentryComponent(), required=False)
    registry.register("pyroscope", PyroscopeComponent(), required=False)
    registry.register("prometheus", PrometheusComponent(), required=False)
    # QueryObserver registered later after factory creates the subsystem

    return registry


async def startup_observability(app: "FastAPI") -> None:
    """Initialize all observability subsystems via the registry."""
    global _registry
    _registry = create_registry()
    statuses = await _registry.start_all()
    app.state.observability_registry = _registry

    # Log startup summary
    started = [s.name for s in statuses if s.started]
    failed = [s.name for s in statuses if not s.started]
    if started:
        logger.info("Observability components started: %s", ", ".join(started))
    if failed:
        logger.info("Observability components skipped: %s", ", ".join(failed))

    logger.info("Starting FastAPI Nexus server...")
    _startup_thread_pool(app)


async def shutdown_observability() -> None:
    """Shutdown all observability components via the registry."""
    global _registry
    if _registry:
        await _registry.shutdown_all()
        _registry = None


def _startup_thread_pool(app: "FastAPI") -> None:
    """Configure thread pool size (Issue #932)."""
    from anyio import to_thread

    limiter = to_thread.current_default_thread_limiter()
    limiter.total_tokens = app.state.thread_pool_size
    logger.info("Thread pool size set to %d", limiter.total_tokens)
