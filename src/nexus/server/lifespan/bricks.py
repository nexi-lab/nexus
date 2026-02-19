"""Brick lifecycle startup/shutdown (Issue #1704) + reconciler (Issue #2059).

Calls ``BrickLifecycleManager.mount_all()`` during server startup and
``unmount_all()`` during shutdown.  Starts the ``BrickReconciler`` as a
background task for self-healing with exponential backoff.

No-ops gracefully when NexusFS or the lifecycle manager are absent.
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
    """Mount all registered bricks and start the reconciler."""
    nx = getattr(app.state, "nexus_fs", None)
    if nx is None:
        return []

    _sys = getattr(nx, "_system_services", None)
    manager = getattr(_sys, "brick_lifecycle_manager", None) if _sys else None
    if manager is None:
        return []

    # Mount all bricks (DAG-ordered, concurrent per level)
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

    # Start the brick reconciler (Issue #2059)
    bg_tasks: list[asyncio.Task[None]] = []
    reconciler = getattr(_sys, "brick_reconciler", None) if _sys else None
    if reconciler is not None:
        try:
            await reconciler.start()
            logger.info("[LIFECYCLE] BrickReconciler started")
            # The reconciler manages its own internal tasks.  We create a
            # sentinel task that waits for cancellation and then stops the
            # reconciler, so the lifespan can cancel it on shutdown.

            async def _reconciler_sentinel() -> None:
                try:
                    await asyncio.Event().wait()  # blocks until cancelled
                except asyncio.CancelledError:
                    await reconciler.stop()

            bg_tasks.append(
                asyncio.create_task(_reconciler_sentinel(), name="brick_reconciler_sentinel")
            )
        except Exception as exc:
            logger.error("[LIFECYCLE] BrickReconciler start failed: %s", exc)

    return bg_tasks


async def shutdown_bricks(app: FastAPI) -> None:
    """Unmount all active bricks in reverse-DAG order."""
    nx = getattr(app.state, "nexus_fs", None)
    if nx is None:
        return

    _sys = getattr(nx, "_system_services", None)
    manager = getattr(_sys, "brick_lifecycle_manager", None) if _sys else None
    if manager is None:
        return

    try:
        report = await manager.unmount_all()
        logger.info(
            "[LIFECYCLE] unmount_all complete: %d remaining active",
            report.active,
        )
    except Exception as exc:
        logger.error("[LIFECYCLE] unmount_all failed: %s", exc)
