"""Lifespan management for the FastAPI Nexus server.

Extracted from fastapi_server.py (#1602). The lifespan orchestrator calls
domain-specific initializers during startup and shuts them down in reverse
order during shutdown.

Each initializer function:
- Accepts ``app: FastAPI`` (reads/writes ``app.state``)
- Returns a list of ``asyncio.Task`` references that must be cancelled on shutdown
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _compute_features_info(app: "FastAPI") -> None:
    """Compute and store features_info on app.state (Issue #1389).

    Called once at startup. The result is immutable and served by
    GET /api/v2/features with O(1) cost.
    """
    from nexus.core.deployment_profile import ALL_BRICK_NAMES, DeploymentProfile
    from nexus.server.api.core.features import FeaturesResponse, PerformanceTuningInfo

    # Read profile from app state (set during server init)
    profile_str: str = getattr(app.state, "deployment_profile", "full")
    try:
        profile = DeploymentProfile(profile_str)
    except ValueError:
        logger.warning("Unknown deployment profile '%s', defaulting to 'full'", profile_str)
        profile = DeploymentProfile.FULL

    # Read enabled_bricks from app state (set during factory wiring)
    enabled: frozenset[str] = getattr(app.state, "enabled_bricks", profile.default_bricks())

    mode: str = getattr(app.state, "deployment_mode", "standalone")

    # Get version
    version: str | None = None
    try:
        from importlib.metadata import version as _get_version

        version = _get_version("nexus-ai-fs")
    except Exception:
        pass

    disabled = sorted(ALL_BRICK_NAMES - enabled)

    # Issue #2071: include performance tuning summary
    _pt = getattr(app.state, "profile_tuning", None)
    _perf_info = None
    if _pt is not None:
        _perf_info = PerformanceTuningInfo(
            thread_pool_size=_pt.concurrency.thread_pool_size,
            default_workers=_pt.concurrency.default_workers,
            task_runner_workers=_pt.concurrency.task_runner_workers,
            default_http_timeout=_pt.network.default_http_timeout,
            db_pool_size=_pt.storage.db_pool_size,
            search_max_concurrency=_pt.search.search_max_concurrency,
        )

    features_info = FeaturesResponse(
        profile=profile.value,
        mode=mode,
        enabled_bricks=sorted(enabled),
        disabled_bricks=disabled,
        version=version,
        performance_tuning=_perf_info,
    )
    app.state.features_info = features_info

    logger.info(
        "Deployment profile=%s, mode=%s, enabled=%d bricks, disabled=%d bricks",
        profile.value,
        mode,
        len(enabled),
        len(disabled),
    )


def _wire_query_observer(app: "FastAPI") -> None:
    """Register QueryObserverComponent into the observability registry.

    Called after startup_services so NexusFS._service_extras is available.
    """
    registry = getattr(app.state, "observability_registry", None)
    nexus_fs = getattr(app.state, "nexus_fs", None)
    if registry is None or nexus_fs is None:
        return

    obs_subsystem = getattr(nexus_fs, "_service_extras", {}).get("observability_subsystem")
    if obs_subsystem is None:
        return

    try:
        from nexus.server.observability.components import QueryObserverComponent

        registry.register("query-observer", QueryObserverComponent(obs_subsystem), required=False)
        logger.info("QueryObserverComponent registered in observability registry")
    except Exception as exc:
        logger.info("QueryObserverComponent registration skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: "FastAPI") -> AsyncIterator[None]:
    """Application lifespan manager.

    Calls domain-specific initializers during startup and tears them
    down in reverse order during shutdown.
    """
    from nexus.server.lifespan.a2a_grpc import shutdown_a2a_grpc, startup_a2a_grpc
    from nexus.server.lifespan.ipc import shutdown_ipc, startup_ipc
    from nexus.server.lifespan.observability import (
        shutdown_observability,
        startup_observability,
    )
    from nexus.server.lifespan.permissions import startup_permissions
    from nexus.server.lifespan.realtime import shutdown_realtime, startup_realtime
    from nexus.server.lifespan.search import startup_search
    from nexus.server.lifespan.services import shutdown_services, startup_services
    from nexus.server.lifespan.uploads import startup_uploads

    # Collect all background tasks for clean shutdown
    bg_tasks: list[asyncio.Task] = []

    # --- Startup (order matters: observability first, then core, then services) ---

    await startup_observability(app)
    _compute_features_info(app)
    bg_tasks.extend(await startup_permissions(app))
    bg_tasks.extend(await startup_realtime(app))
    bg_tasks.extend(await startup_search(app))
    bg_tasks.extend(await startup_services(app))
    bg_tasks.extend(await startup_uploads(app))
    bg_tasks.extend(await startup_ipc(app))
    bg_tasks.extend(await startup_a2a_grpc(app))

    # Wire QueryObserverComponent into registry after services start (Issue #2072)
    _wire_query_observer(app)

    yield

    # --- Shutdown (reverse order) ---
    logger.info("Shutting down FastAPI Nexus server...")

    # Cancel all background tasks first
    for task in bg_tasks:
        if task and not task.done():
            task.cancel()
    if bg_tasks:
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*[t for t in bg_tasks if t], return_exceptions=True)
        logger.debug(f"Cancelled {len(bg_tasks)} background tasks")

    await shutdown_a2a_grpc(app)
    await shutdown_ipc(app)
    await shutdown_services(app)
    await shutdown_realtime(app)

    # Close NexusFS kernel
    if app.state.nexus_fs:
        # Stop WriteBuffer to drain pending events before closing kernel (Issue #1370)
        _wo = getattr(app.state.nexus_fs, "_write_observer", None)
        if _wo is not None and hasattr(_wo, "stop"):
            try:
                _wo.stop()
                logger.info("WriteBuffer stopped")
            except Exception as e:
                logger.warning(f"Error stopping WriteBuffer: {e}")

        if hasattr(app.state.nexus_fs, "close"):
            app.state.nexus_fs.close()

    # Shutdown cache factory (Issue #1075)
    if hasattr(app.state, "cache_factory") and app.state.cache_factory:
        try:
            await app.state.cache_factory.shutdown()
            logger.info("Cache factory stopped")
        except Exception as e:
            logger.warning(f"Error shutting down cache factory: {e}")

    await shutdown_observability()
