"""Realtime startup/shutdown: event bus, WebSocket, WriteBack, locks.

Extracted from fastapi_server.py (#1602).
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_realtime(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Initialize realtime infrastructure and return background tasks.

    Covers:
    - Event bus start + wiring
    - Event Stream Exporters (Issue #1138)
    - WebSocket Manager (Issue #1116)
    - WriteBack Service (Issue #1129)
    - Lock Manager coordination (Issue #1186)

    Services that implement PersistentService (start/stop) are registered
    with the coordinator for auto-lifecycle management.
    """
    bg_tasks: list[asyncio.Task] = []
    coord = svc.service_coordinator

    await _startup_event_bus(app, svc, coord)
    await _startup_exporter_registry(app, svc, coord)
    await _startup_websocket(app, svc, coord)
    await _startup_writeback(app, svc, coord)
    await _startup_lock_manager(app, svc, coord)

    # Enlist subscription_manager (Q1 — static, no lifecycle)
    sub_mgr = getattr(app.state, "subscription_manager", None)
    if sub_mgr is not None and coord is not None:
        await coord.enlist("subscription_manager", sub_mgr)

    return bg_tasks


async def shutdown_realtime(app: "FastAPI", svc: "LifespanServices") -> None:
    """Shutdown realtime infrastructure.

    PersistentService instances (EventBus, WebSocketManager, WriteBackService)
    are stopped automatically by the coordinator via ``aclose()``.
    Only non-PersistentService cleanup remains here.
    """
    # Disconnect Lock Manager coordination client (Q1, manual)
    coord_client = svc.coordination_client
    if svc.nexus_fs and coord_client is not None:
        try:
            await coord_client.disconnect()
            logger.info("Lock manager coordination client disconnected")
        except Exception as e:
            logger.warning("Error disconnecting coordination client: %s", e, exc_info=True)

    # Close ExporterRegistry (Q1, manual cleanup)
    exporter_registry = app.state.exporter_registry
    if exporter_registry is not None:
        try:
            await exporter_registry.close_all()
            logger.info("Exporter registry closed")
        except Exception as e:
            logger.warning("Error closing exporter registry: %s", e, exc_info=True)

    # subscription_manager (Q1) — close + clear singleton
    if app.state.subscription_manager:
        await app.state.subscription_manager.close()
        from nexus.server.subscriptions import set_subscription_manager

        set_subscription_manager(None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _startup_event_bus(
    _app: "FastAPI",
    svc: "LifespanServices",
    coord: Any,
) -> None:
    """Start event bus (Q3 — PersistentService)."""
    if not svc.nexus_fs:
        return

    event_bus_ref = svc.event_bus
    if event_bus_ref is None:
        return

    # Connect the underlying DragonflyClient (async init required before start)
    redis_client = getattr(event_bus_ref, "_redis", None)
    if redis_client and hasattr(redis_client, "connect"):
        try:
            await redis_client.connect()
        except Exception as e:
            logger.warning("Failed to connect event bus Redis client: %s", e)

    # Enlist: coordinator auto-detects Q3 → calls start()
    if coord is not None:
        await coord.enlist("event_bus", event_bus_ref)
    elif hasattr(event_bus_ref, "start") and not getattr(event_bus_ref, "_started", True):
        await event_bus_ref.start()
        logger.info("Event bus started (no coordinator)")

    # Issue #1331: Store main event loop ref for cross-thread event publishing
    svc.nexus_fs._main_event_loop = asyncio.get_running_loop()


async def _startup_websocket(
    app: "FastAPI",
    svc: "LifespanServices",
    coord: Any,
) -> None:
    """Initialize WebSocket Manager (Q3 — PersistentService)."""
    try:
        from nexus.server.websocket import WebSocketManager

        ws_mgr = WebSocketManager(
            event_bus=svc.event_bus,
            reactive_manager=app.state.reactive_subscription_manager,
        )
        app.state.websocket_manager = ws_mgr

        if coord is not None:
            await coord.enlist("websocket_manager", ws_mgr, depends_on=("event_bus",))
        else:
            await ws_mgr.start()
    except Exception as e:
        logger.warning("Failed to start WebSocket manager: %s", e)


async def _startup_writeback(
    app: "FastAPI",
    svc: "LifespanServices",
    coord: Any,
) -> None:
    """Initialize WriteBack Service (Q3 — PersistentService).

    When ``NEXUS_WRITE_BACK=true`` and an event bus is available, the full
    WriteBackService starts with conflict resolution and backlog stores.

    Otherwise, an ``InMemoryWriteBack`` fallback is installed so that sync
    endpoints return zero-change responses instead of 503 — this keeps
    the API surface consistent in standalone mode.
    """
    if not svc.nexus_fs:
        return

    write_back_enabled = os.getenv("NEXUS_WRITE_BACK", "").lower() in ("true", "1", "yes")

    try:
        if write_back_enabled:
            from nexus.system_services.gateway import NexusFSGateway
            from nexus.system_services.sync.change_log_store import ChangeLogStore
            from nexus.system_services.sync.conflict_log_store import ConflictLogStore
            from nexus.system_services.sync.conflict_resolution import ConflictStrategy
            from nexus.system_services.sync.sync_backlog_store import SyncBacklogStore
            from nexus.system_services.sync.write_back_service import WriteBackService

            gw = NexusFSGateway(svc.nexus_fs)

            _is_pg = gw.is_postgresql
            conflict_log_store = ConflictLogStore(
                record_store=gw.record_store, is_postgresql=_is_pg
            )
            app.state.conflict_log_store = conflict_log_store
            # Enlist conflict_log_store (Q1 — static, no lifecycle)
            if coord is not None:
                await coord.enlist("conflict_log_store", conflict_log_store)

            wb_event_bus = svc.event_bus
            if wb_event_bus:
                backlog_store = SyncBacklogStore(record_store=gw.record_store, is_postgresql=_is_pg)
                change_log_store = ChangeLogStore(
                    record_store=gw.record_store, is_postgresql=_is_pg
                )

                _policy_map = {
                    "lww": ConflictStrategy.KEEP_NEWER,
                    "fork": ConflictStrategy.RENAME_CONFLICT,
                }
                raw_policy = os.getenv("NEXUS_CONFLICT_POLICY", "keep_newer")
                try:
                    default_strategy = ConflictStrategy(raw_policy)
                except ValueError:
                    default_strategy = _policy_map.get(raw_policy, ConflictStrategy.KEEP_NEWER)

                wb_svc = WriteBackService(
                    gateway=gw,
                    event_bus=wb_event_bus,
                    backlog_store=backlog_store,
                    change_log_store=change_log_store,
                    default_strategy=default_strategy,
                    conflict_log_store=conflict_log_store,
                )
                app.state.write_back_service = wb_svc

                if coord is not None:
                    await coord.enlist(
                        "write_back",
                        wb_svc,
                        depends_on=("event_bus",),
                    )
                else:
                    await wb_svc.start()
                return

        # Fallback: InMemoryWriteBack (structurally PersistentService, start/stop are no-ops)
        from nexus.contracts.protocols.write_back import InMemoryWriteBack

        wb_fallback = InMemoryWriteBack()
        app.state.write_back_service = wb_fallback

        if coord is not None:
            await coord.enlist("write_back", wb_fallback)
        else:
            await wb_fallback.start()
    except Exception as e:
        logger.warning("Failed to start WriteBack service: %s", e)


async def _startup_lock_manager(
    _app: "FastAPI",
    svc: "LifespanServices",
    coord: Any,
) -> None:
    """Connect Lock Manager coordination client (Q1 — no lifecycle)."""
    if not svc.nexus_fs:
        return

    coord_client = svc.coordination_client
    if coord_client is None:
        return

    # Q1: register for discoverability, no start/stop
    if coord is not None:
        await coord.enlist("coordination_client", coord_client)

    try:
        await coord_client.connect()
        logger.info("Lock manager coordination client connected")
    except Exception as e:
        logger.warning("Failed to connect lock manager coordination client: %s", e)


async def _startup_exporter_registry(
    app: "FastAPI",
    _svc: "LifespanServices",
    coord: Any,
) -> None:
    """Initialize ExporterRegistry (Q1 — no lifecycle, manual cleanup)."""
    app.state.exporter_registry = None

    try:
        from nexus.system_services.event_log.exporter_registry import ExporterRegistry
        from nexus.system_services.event_log.exporters.config import EventStreamConfig
        from nexus.system_services.event_log.exporters.factory import create_exporter

        enabled = os.getenv("NEXUS_EVENT_STREAM_ENABLED", "").lower() in ("true", "1", "yes")
        if not enabled:
            logger.debug("Event stream export disabled (NEXUS_EVENT_STREAM_ENABLED not set)")
            return

        exporter_type = os.getenv("NEXUS_EVENT_STREAM_EXPORTER", "kafka")
        config = EventStreamConfig(enabled=True, exporter=exporter_type)

        registry = ExporterRegistry()
        exporter = create_exporter(config)
        if exporter is not None:
            registry.register(exporter)

        app.state.exporter_registry = registry

        # Q1: register for discoverability
        if coord is not None:
            await coord.enlist("exporter_registry", registry)

        logger.info(
            "ExporterRegistry initialized (exporters=%s)",
            registry.exporter_names,
        )
    except Exception as e:
        logger.warning("Failed to initialize ExporterRegistry: %s", e)
