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

    from nexus.contracts.cache_store import CacheStoreABC
    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


def _resolve_ipc_cache_store(app: "FastAPI", svc: "LifespanServices") -> "CacheStoreABC | None":
    """Resolve the cache store used by IPC pub/sub.

    Prefer the runtime CacheBrick when it has a real backend. That avoids
    binding IPC to a stale NullCacheStore held on NexusFS when permissions
    startup created a working Dragonfly-backed CacheBrick separately.
    """
    cache_brick = getattr(app.state, "cache_brick", None)
    if cache_brick is None and svc.nexus_fs is not None:
        _svc_fn = getattr(svc.nexus_fs, "service", None)
        cache_brick = _svc_fn("cache_brick") if _svc_fn else None

    if cache_brick is not None and getattr(cache_brick, "has_cache_store", False):
        return getattr(cache_brick, "cache_store", None)

    return getattr(svc.nexus_fs, "cache_store", None)


async def startup_ipc(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Start IPC background tasks (TTLSweeper + DT_PIPE wakeup).

    Reads ``ipc_provisioner`` from ServiceRegistry and exposes it
    on ``app.state`` for the IPC REST router.

    Returns list of background tasks to cancel on shutdown.
    """
    bg_tasks: list[asyncio.Task] = []

    if svc.nexus_fs is None:
        return bg_tasks

    _svc_fn = getattr(svc.nexus_fs, "service", None)
    if _svc_fn is None:
        return bg_tasks

    ipc_provisioner = _svc_fn("ipc_provisioner")

    if ipc_provisioner is None:
        logger.debug("[IPC] IPC provisioner not available, skipping IPC startup")
        return bg_tasks

    # Derive the IPC zone from the provisioner so the sweeper / event channels
    # agree with `/agents` mount metadata and with AgentProvisioner. In
    # federated / multi-zone setups ``svc.zone_id`` may legitimately diverge
    # from the IPC zone, so falling back to it first would split-brain the
    # sweeper from the provisioning path.
    _prov_zone = getattr(ipc_provisioner, "zone_id", None)
    zone_id = _prov_zone or svc.zone_id or ROOT_ZONE_ID

    # --- Issue #3197: EventPublisher via CacheStore pub/sub ---
    cache_store = _resolve_ipc_cache_store(app, svc)
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

    # Expose IPC services on app.state for REST router access
    app.state.ipc_nexus_fs = svc.nexus_fs
    app.state.ipc_provisioner = ipc_provisioner
    app.state.ipc_cache_store = cache_store
    app.state.ipc_event_publisher = event_publisher

    # Start TTLSweeper background task (with event-driven pub/sub if cache_store available)
    try:
        from nexus.bricks.ipc.sweep import TTLSweeper

        sweeper = TTLSweeper(
            vfs=svc.nexus_fs,
            zone_id=zone_id,
            interval=60,
            cache_store=cache_store,
        )
        app.state.ipc_sweeper = sweeper
        nx = svc.nexus_fs if hasattr(svc, "nexus_fs") else svc
        if hasattr(nx, "sys_setattr"):
            nx.sys_setattr(
                "/__sys__/services/ipc_sweeper",
                service=sweeper,
            )
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
