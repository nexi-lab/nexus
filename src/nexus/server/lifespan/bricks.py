"""Brick lifecycle startup/shutdown (Issue #1704).

Calls ``BrickLifecycleManager.mount_all()`` during server startup and
``unmount_all()`` during shutdown.  No-ops gracefully when NexusFS or the
lifecycle manager are absent.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_bricks(_app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task[None]]:
    """Mount all registered bricks via BrickLifecycleManager."""
    if svc.nexus_fs is None:
        return []

    manager = svc.brick_lifecycle_manager
    if manager is None:
        return []

    try:
        t0 = time.perf_counter()
        report = await manager.mount_all()
        elapsed = time.perf_counter() - t0
        logger.info(
            "[LIFECYCLE] mount_all complete: %d/%d active, %d failed (%.3fs)",
            report.active,
            report.total,
            report.failed,
            elapsed,
        )
    except Exception as exc:
        logger.error("[LIFECYCLE] mount_all failed: %s", exc)

    # Start brick reconciler (Issue #2060)
    reconciler = svc.brick_reconciler
    if reconciler is not None:
        try:
            coord = svc.service_coordinator
            if coord is not None:
                await coord.enlist("brick_reconciler", reconciler)
            else:
                await reconciler.start()
        except Exception as exc:
            logger.warning("[RECONCILER] Failed to start: %s", exc)

    return []


async def shutdown_bricks(_app: "FastAPI", svc: "LifespanServices") -> None:
    """Unmount all active bricks in reverse-DAG order."""
    if svc.nexus_fs is None:
        return

    manager = svc.brick_lifecycle_manager
    if manager is None:
        return

    # brick_reconciler (Q3) — stopped by coordinator via aclose()

    try:
        report = await manager.unmount_all()
        logger.info(
            "[LIFECYCLE] unmount_all complete: %d remaining active",
            report.active,
        )
    except Exception as exc:
        logger.error("[LIFECYCLE] unmount_all failed: %s", exc)
