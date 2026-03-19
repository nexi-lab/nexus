"""IPC Brick lifespan: TTLSweeper background task.

Issue: #1727, LEGO §8: Filesystem-as-IPC.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_ipc(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Start IPC background tasks (TTLSweeper).

    Reads ``ipc_storage_driver`` and ``ipc_provisioner`` from
    ``svc.brick_services`` and exposes them on ``app.state``
    for the IPC REST router.

    Returns list of background tasks to cancel on shutdown.
    """
    bg_tasks: list[asyncio.Task] = []

    if svc.nexus_fs is None:
        return bg_tasks

    brk = svc.brick_services
    if brk is None:
        return bg_tasks

    ipc_storage = getattr(brk, "ipc_storage_driver", None)
    ipc_provisioner = getattr(brk, "ipc_provisioner", None)

    if ipc_storage is None:
        logger.debug("[IPC] IPC storage driver not available, skipping IPC startup")
        return bg_tasks

    # Expose IPC services on app.state for REST router access
    app.state.ipc_storage_driver = ipc_storage
    app.state.ipc_provisioner = ipc_provisioner

    # Enlist IPC driver + provisioner (Q1 — restart-required, no lifecycle)
    coord = svc.service_coordinator
    if coord is not None:
        await coord.enlist("ipc_storage_driver", ipc_storage)
        if ipc_provisioner is not None:
            await coord.enlist("ipc_provisioner", ipc_provisioner)

    zone_id = svc.zone_id or ROOT_ZONE_ID

    # Start TTLSweeper background task
    try:
        from nexus.bricks.ipc.sweep import TTLSweeper

        sweeper = TTLSweeper(
            storage=ipc_storage,
            zone_id=zone_id,
            interval=60,
        )
        app.state.ipc_sweeper = sweeper
        if coord is not None:
            await coord.enlist("ipc_sweeper", sweeper)
        else:
            await sweeper.start()  # creates internal asyncio.Task
        logger.info("[IPC] TTLSweeper started (zone=%s)", zone_id)
    except Exception as exc:
        logger.warning("[IPC] TTLSweeper unavailable: %s", exc)

    logger.info("[IPC] IPC brick ready (zone=%s)", zone_id)
    return bg_tasks


async def shutdown_ipc(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop IPC background tasks.

    ipc_sweeper (Q3) — stopped by coordinator via aclose().
    """
