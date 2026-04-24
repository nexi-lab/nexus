"""Lifespan management for the FastAPI Nexus server.

Extracted from fastapi_server.py (#1602). The lifespan orchestrator calls
domain-specific initializers during startup and shuts them down in reverse
order during shutdown.

Each initializer function:
- Accepts ``app: FastAPI`` and ``svc: LifespanServices``
- Returns a list of ``asyncio.Task`` references that must be cancelled on shutdown
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from nexus.server.lifespan.services_container import LifespanServices

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _compute_features_info(app: "FastAPI", svc: LifespanServices) -> None:
    """Compute and store features_info on app.state (Issue #1389).

    Called once at startup. The result is immutable and served by
    GET /api/v2/features with O(1) cost.
    """
    from nexus.contracts.deployment_profile import ALL_BRICK_NAMES, DeploymentProfile
    from nexus.server.api.core.features import FeaturesResponse, PerformanceTuningInfo

    # Read profile from svc (set during server init).
    profile_str: str = svc.deployment_profile
    try:
        profile = DeploymentProfile(profile_str)
    except ValueError:
        logger.warning("Unknown deployment profile '%s', defaulting to 'full'", profile_str)
        profile = DeploymentProfile.FULL

    # Read enabled_bricks from svc (set during factory wiring)
    enabled: frozenset[str] = svc.enabled_bricks or profile.default_bricks()

    mode: str = svc.deployment_mode

    # Get version
    version: str | None = None
    try:
        from importlib.metadata import version as _get_version

        version = _get_version("nexus-ai-fs")
    except Exception:
        version = "unknown"

    disabled = sorted(ALL_BRICK_NAMES - enabled)

    # Issue #2071: include performance tuning summary
    _pt = svc.profile_tuning
    _perf_info = None
    if _pt is not None:
        _perf_info = PerformanceTuningInfo(
            thread_pool_size=_pt.concurrency.thread_pool_size,
            default_workers=_pt.concurrency.default_workers,
            task_runner_workers=_pt.concurrency.task_runner_workers,
            default_http_timeout=_pt.network.default_http_timeout,
            db_pool_size=_pt.storage.db_pool_size,
            search_max_concurrency=_pt.search.search_max_concurrency,
            heartbeat_flush_interval=_pt.background_task.heartbeat_flush_interval,
            default_max_retries=_pt.resiliency.default_max_retries,
            blob_operation_timeout=_pt.connector.blob_operation_timeout,
            asyncpg_max_size=_pt.pool.asyncpg_max_size,
        )

    # Detect rate limiting status from env (same var used in fastapi_server.py)
    import os

    _rate_limit_enabled = os.environ.get("NEXUS_RATE_LIMIT_ENABLED", "").lower() in (
        "true",
        "1",
        "yes",
    )

    features_info = FeaturesResponse(
        profile=profile.value,
        mode=mode,
        enabled_bricks=sorted(enabled),
        disabled_bricks=disabled,
        version=version,
        performance_tuning=_perf_info,
        rate_limit_enabled=_rate_limit_enabled,
    )
    app.state.features_info = features_info

    logger.info(
        "Deployment profile=%s, mode=%s, enabled=%d bricks, disabled=%d bricks",
        profile.value,
        mode,
        len(enabled),
        len(disabled),
    )


def _wire_query_observer(_app: "FastAPI", svc: LifespanServices) -> None:
    """Register QueryObserverComponent into the observability registry.

    Called after startup_services so observability_subsystem is available.
    """
    registry = svc.observability_registry
    if registry is None:
        return

    obs_subsystem = svc.observability_subsystem
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
    from nexus.grpc.server import shutdown_grpc, startup_grpc
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

    # Extract typed service container once
    svc = LifespanServices.from_app(app)

    # Issue #2168: startup tracker for health probes
    from nexus.server.health.startup_tracker import StartupPhase

    tracker = getattr(app.state, "startup_tracker", None)

    def _done(phase: StartupPhase) -> None:
        if tracker is not None:
            tracker.complete(phase)

    # --- Startup (order matters: bootstrap first, then observability, then services) ---

    # NexusFS lifecycle Phase 3: start async tasks owned by NexusFS
    nx = getattr(app.state, "nexus_fs", None)
    if nx is not None and hasattr(nx, "bootstrap"):
        nx.bootstrap()

    await startup_observability(app, svc)
    # Re-extract observability_registry after startup_observability writes it
    svc.observability_registry = getattr(app.state, "observability_registry", None)

    # Configure thread pool size (Issue #932) — server infra, not observability
    from anyio import to_thread

    limiter = to_thread.current_default_thread_limiter()
    limiter.total_tokens = svc.thread_pool_size
    logger.info("Thread pool size set to %d", limiter.total_tokens)

    _done(StartupPhase.OBSERVABILITY)

    _compute_features_info(app, svc)
    _done(StartupPhase.FEATURES)

    bg_tasks.extend(await startup_permissions(app, svc))
    _done(StartupPhase.PERMISSIONS)

    bg_tasks.extend(await startup_realtime(app, svc))
    _done(StartupPhase.REALTIME)

    bg_tasks.extend(await startup_search(app, svc))
    _done(StartupPhase.SEARCH)

    bg_tasks.extend(await startup_services(app, svc))
    _done(StartupPhase.SERVICES)

    bg_tasks.extend(await startup_uploads(app, svc))
    _done(StartupPhase.UPLOADS)

    bg_tasks.extend(await startup_ipc(app, svc))
    _done(StartupPhase.IPC)

    bg_tasks.extend(await startup_grpc(app, svc))
    _done(StartupPhase.GRPC)

    # Wire QueryObserverComponent into registry after services start (Issue #2072)
    _wire_query_observer(app, svc)

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
        logger.debug("Cancelled %d background tasks", len(bg_tasks))

    await shutdown_grpc(app, svc)
    await shutdown_ipc(app, svc)
    await shutdown_services(app, svc)
    await shutdown_realtime(app, svc)

    # Stop durable invalidation stream (Issue #3396) — before NexusFS close
    _durable = getattr(app.state, "durable_stream", None)
    if _durable is not None:
        await _durable.stop()
        logger.debug("Durable invalidation stream stopped")

    # Close NexusFS kernel (sync shutdown for PersistentService + hooks)
    if app.state.nexus_fs:
        if hasattr(app.state.nexus_fs, "aclose"):
            app.state.nexus_fs.aclose()
        elif hasattr(app.state.nexus_fs, "close"):
            app.state.nexus_fs.close()

    # CacheBrick stop is now handled by coordinator via aclose() (enlisted as BackgroundService)

    await shutdown_observability(app, svc)
