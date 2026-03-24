"""IPC Brick lifespan: TTLSweeper + DT_PIPE wakeup + CacheStore pub/sub.

Issue: #1727, LEGO §8: Filesystem-as-IPC.
Issue: #3197: DT_PIPE wakeup + event-driven TTL sweeping.
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
    """Start IPC background tasks (TTLSweeper + DT_PIPE wakeup).

    Reads ``ipc_storage_driver`` and ``ipc_provisioner`` from
    ``svc.brick_services`` and exposes them on ``app.state``
    for the IPC REST router.

    Issue #3197:
      - Creates PipeWakeupNotifier + PipeNotifyFactory from PipeManager
      - Passes cache_store to TTLSweeper for event-driven pub/sub sweeping
      - Exposes wakeup_notifiers on app.state for MessageSender in REST router

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

    zone_id = svc.zone_id or ROOT_ZONE_ID

    # --- Issue #3197: DT_PIPE wakeup + EventPublisher + CacheStore ---
    wakeup_notifiers = []
    cache_store = getattr(svc.nexus_fs, "cache_store", None)
    event_publisher = None

    # Wire EventPublisher via CacheStore pub/sub so MessageSender publishes
    # ipc.inbox.{agent_id} events that MessageProcessor can subscribe to.
    if cache_store is not None:
        try:
            from nexus.bricks.ipc.wakeup import CacheStoreEventPublisher

            event_publisher = CacheStoreEventPublisher(cache_store)
            logger.info("[IPC] EventPublisher wired via CacheStore pub/sub")
        except Exception as exc:
            logger.warning("[IPC] EventPublisher unavailable: %s", exc)

    if svc.pipe_manager is not None:
        try:
            from nexus.bricks.ipc.wakeup import PipeNotifyFactory, PipeWakeupNotifier

            notifier = PipeWakeupNotifier(svc.pipe_manager)
            wakeup_notifiers.append(notifier)

            # Inject notify pipe factory into provisioner (late-binding)
            notify_factory = PipeNotifyFactory(svc.pipe_manager)
            if ipc_provisioner is not None:
                ipc_provisioner._notify_pipe_factory = notify_factory

            logger.info("[IPC] DT_PIPE wakeup notifier + notify factory wired")
        except Exception as exc:
            logger.warning("[IPC] DT_PIPE wakeup unavailable: %s", exc)

    # Expose IPC services on app.state for REST router access
    app.state.ipc_storage_driver = ipc_storage
    app.state.ipc_provisioner = ipc_provisioner
    app.state.ipc_wakeup_notifiers = wakeup_notifiers
    app.state.ipc_cache_store = cache_store
    app.state.ipc_event_publisher = event_publisher
    # Create MessageProcessorRegistry with pipe_manager for receiver-side
    # DT_PIPE wakeup (Issue #3197). Agent runtimes use
    # registry.create_processor() to get a MessageProcessor with
    # PipeWakeupListener auto-wired.
    try:
        from nexus.bricks.ipc.registry import MessageProcessorRegistry

        processor_registry = MessageProcessorRegistry(pipe_manager=svc.pipe_manager)
        app.state.ipc_processor_registry = processor_registry
        if svc.pipe_manager is not None:
            logger.info("[IPC] MessageProcessorRegistry wired with DT_PIPE wakeup")
    except Exception as exc:
        logger.warning("[IPC] MessageProcessorRegistry unavailable: %s", exc)
    app.state.ipc_pipe_manager = svc.pipe_manager

    # Enlist IPC driver + provisioner (Q1 — restart-required, no lifecycle)
    coord = svc.service_coordinator
    if coord is not None:
        await coord.enlist("ipc_storage_driver", ipc_storage)
        if ipc_provisioner is not None:
            await coord.enlist("ipc_provisioner", ipc_provisioner)

    # Start TTLSweeper background task (with event-driven pub/sub if cache_store available)
    try:
        from nexus.bricks.ipc.sweep import TTLSweeper

        sweeper = TTLSweeper(
            storage=ipc_storage,
            zone_id=zone_id,
            interval=60,
            cache_store=cache_store,
        )
        app.state.ipc_sweeper = sweeper
        if coord is not None:
            await coord.enlist("ipc_sweeper", sweeper)
        else:
            await sweeper.start()  # creates internal asyncio.Task
        logger.info(
            "[IPC] TTLSweeper started (zone=%s, event_driven=%s)",
            zone_id,
            cache_store is not None,
        )
    except Exception as exc:
        logger.warning("[IPC] TTLSweeper unavailable: %s", exc)

    logger.info("[IPC] IPC brick ready (zone=%s)", zone_id)
    return bg_tasks


async def shutdown_ipc(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop IPC background tasks.

    ipc_sweeper (Q3) — stopped by coordinator via aclose().
    """
