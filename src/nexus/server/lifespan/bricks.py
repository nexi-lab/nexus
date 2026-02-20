"""Brick lifecycle startup/shutdown (Issue #1704).

Calls ``BrickLifecycleManager.mount_all()`` during server startup and
``unmount_all()`` during shutdown.  No-ops gracefully when NexusFS or the
lifecycle manager are absent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_bricks(app: FastAPI) -> list[asyncio.Task[None]]:
    """Mount all registered bricks via BrickLifecycleManager."""
    if app.state.nexus_fs is None:
        return []

    manager = app.state.brick_lifecycle_manager
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
    reconciler = app.state.brick_reconciler
    if reconciler is not None:
        try:
            await reconciler.start()
        except Exception as exc:
            logger.warning("[RECONCILER] Failed to start: %s", exc)

    return []


async def shutdown_bricks(app: FastAPI) -> None:
    """Unmount all active bricks in reverse-DAG order."""
    if app.state.nexus_fs is None:
        return

    manager = app.state.brick_lifecycle_manager
    if manager is None:
        return

    # Stop brick reconciler before unmounting (Issue #2060)
    reconciler = app.state.brick_reconciler
    if reconciler is not None:
        try:
            await reconciler.stop()
            logger.info("[RECONCILER] Stopped")
        except Exception as exc:
            logger.warning("[RECONCILER] Failed to stop: %s", exc)

    try:
        report = await manager.unmount_all()
        logger.info(
            "[LIFECYCLE] unmount_all complete: %d remaining active",
            report.active,
        )
    except Exception as exc:
        logger.error("[LIFECYCLE] unmount_all failed: %s", exc)
