"""Observability startup/shutdown via ObservabilityRegistry.

Issue #2072: Consolidate observability init/shutdown into unified registry.
Replaces 6 inline _startup_* / 3 shutdown_* calls with a single registry.
"""

import importlib
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.server.observability.registry import ObservabilityRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)

# Declarative registration table: (name, module_path, setup_function, shutdown_function)
_OBSERVABILITY_PROVIDERS: list[tuple[str, str, str, str]] = [
    ("logging", "nexus.server.logging_config", "configure_logging", "shutdown_logging"),
    ("otel-tracing", "nexus.server.telemetry", "setup_telemetry", "shutdown_telemetry"),
    ("sentry", "nexus.server.sentry", "setup_sentry", "shutdown_sentry"),
    ("pyroscope", "nexus.server.profiling", "setup_profiling", "shutdown_profiling"),
    ("prometheus", "nexus.server.metrics", "setup_prometheus", "shutdown_prometheus"),
]


def _make_start(mod: str, fn: str) -> Callable[..., None]:
    """Factory for lazy-import start functions (avoids closure-over-loop-variable bugs)."""

    def _start(**kwargs: Any) -> None:
        module = importlib.import_module(mod)
        getattr(module, fn)(**kwargs)

    return _start


def _make_stop(mod: str, fn: str) -> Callable[[], None]:
    """Factory for lazy-import stop functions (avoids closure-over-loop-variable bugs)."""

    def _stop() -> None:
        module = importlib.import_module(mod)
        getattr(module, fn)()

    return _stop


def create_registry(*, write_observer: Any = None) -> ObservabilityRegistry:
    """Create and populate the observability registry.

    Registration order = startup order (dependencies first).

    Args:
        write_observer: Optional write observer instance for WriteBufferComponent.
    """
    from nexus.server.observability.components import FunctionPairComponent, WriteBufferComponent

    registry = ObservabilityRegistry()
    env = os.environ.get("NEXUS_ENV", "dev")

    for comp_name, module_path, setup_fn_name, shutdown_fn_name in _OBSERVABILITY_PROVIDERS:
        start_kwargs: dict[str, Any] = {"env": env} if comp_name == "logging" else {}
        registry.register(
            comp_name,
            FunctionPairComponent(
                comp_name,
                start_fn=_make_start(module_path, setup_fn_name),
                stop_fn=_make_stop(module_path, shutdown_fn_name),
                start_kwargs=start_kwargs,
            ),
            required=False,
        )

    # WriteBuffer (Issue #1370) — managed shutdown, started by factory
    if write_observer is not None:
        registry.register("write-buffer", WriteBufferComponent(write_observer), required=False)

    # QueryObserver registered later after factory creates the subsystem

    return registry


async def startup_observability(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize all observability subsystems via the registry."""
    write_observer = svc.write_observer
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


async def shutdown_observability(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Shutdown all observability components via the registry."""
    registry = app.state.observability_registry
    if registry:
        await registry.shutdown_all()
        app.state.observability_registry = None
