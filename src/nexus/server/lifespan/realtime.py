"""Realtime startup/shutdown: event bus, WebSocket, WriteBack, locks.

Extracted from fastapi_server.py (#1602).
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """
    bg_tasks: list[asyncio.Task] = []

    await _startup_event_bus(app, svc)
    _startup_exporter_registry(app, svc)
    await _startup_websocket(app, svc)
    await _startup_writeback(app, svc)
    await _startup_connector_sync(app, svc)
    await _startup_lock_manager(app, svc)

    return bg_tasks


async def shutdown_realtime(app: "FastAPI", svc: "LifespanServices") -> None:
    """Shutdown realtime infrastructure in reverse order."""
    # Disconnect Lock Manager coordination client (Issue #1186)
    coord_client = svc.coordination_client
    if svc.nexus_fs and coord_client is not None:
        try:
            await coord_client.disconnect()
            logger.info("Lock manager coordination client disconnected")
        except Exception as e:
            logger.warning("Error disconnecting coordination client: %s", e, exc_info=True)

    # Shutdown WebSocket manager (Issue #1116)
    if app.state.websocket_manager:
        try:
            await app.state.websocket_manager.stop()
            logger.info("WebSocket manager stopped")
        except Exception as e:
            logger.warning("Error shutting down WebSocket manager: %s", e, exc_info=True)

    # Stop WriteBack Service (Issue #1129) and unregister OBSERVE hook (#3194)
    if app.state.write_back_service:
        try:
            # Unregister OBSERVE hook to prevent duplicate observers on hot reload
            _dispatch = getattr(svc.nexus_fs, "_dispatch", None) if svc.nexus_fs else None
            if _dispatch is not None:
                _dispatch.unregister_observe(app.state.write_back_service)
            await app.state.write_back_service.stop()
            logger.info("WriteBack service stopped")
        except Exception as e:
            logger.warning("Error shutting down WriteBack service: %s", e, exc_info=True)

    # Stop event bus (Issue #1331)
    _ebus = svc.event_bus
    if _ebus is not None:
        try:
            await _ebus.stop()
            logger.info("Event bus stopped")
        except Exception as e:
            logger.warning("Error shutting down event bus: %s", e, exc_info=True)

    # Close ExporterRegistry (Issue #1138)
    exporter_registry = app.state.exporter_registry
    if exporter_registry is not None:
        try:
            await exporter_registry.close_all()
            logger.info("Exporter registry closed")
        except Exception as e:
            logger.warning("Error closing exporter registry: %s", e, exc_info=True)

    # Close subscription manager
    if app.state.subscription_manager:
        await app.state.subscription_manager.close()
        from nexus.server.subscriptions import set_subscription_manager

        set_subscription_manager(None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _startup_event_bus(_app: "FastAPI", svc: "LifespanServices") -> None:
    """Start event bus."""
    if not svc.nexus_fs:
        return

    event_bus_ref = svc.event_bus
    if event_bus_ref is None:
        return

    # Connect the underlying DragonflyClient (async init required)
    redis_client = getattr(event_bus_ref, "_redis", None)
    if redis_client and hasattr(redis_client, "connect"):
        try:
            await redis_client.connect()
        except Exception as e:
            logger.warning("Failed to connect event bus Redis client: %s", e)

    # Start the event bus
    if hasattr(event_bus_ref, "start") and not getattr(event_bus_ref, "_started", True):
        try:
            await event_bus_ref.start()
            logger.info("Event bus started for event publishing")
        except Exception as e:
            logger.warning("Failed to start event bus: %s", e)

    # Issue #1331: Store main event loop ref for cross-thread event publishing
    svc.nexus_fs._main_event_loop = asyncio.get_running_loop()


async def _startup_websocket(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize WebSocket Manager for real-time events (Issue #1116)."""
    try:
        from nexus.server.websocket import WebSocketManager

        event_bus = svc.event_bus

        app.state.websocket_manager = WebSocketManager(
            event_bus=event_bus,
            reactive_manager=app.state.reactive_subscription_manager,
        )
        await app.state.websocket_manager.start()
        logger.info("WebSocket manager started for real-time events")
    except Exception as e:
        logger.warning("Failed to start WebSocket manager: %s", e)


async def _startup_writeback(app: "FastAPI", svc: "LifespanServices") -> None:
    """Initialize WriteBack Service for bidirectional sync (Issue #1129/#1130).

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
        # Always initialize ConflictLogStore for the REST API, even when write-back is disabled
        from nexus.services.gateway import NexusFSGateway
        from nexus.services.sync.conflict_log_store import ConflictLogStore

        gw = NexusFSGateway(svc.nexus_fs)
        _is_pg = gw.is_postgresql
        conflict_log_store = ConflictLogStore(record_store=gw.record_store, is_postgresql=_is_pg)
        app.state.conflict_log_store = conflict_log_store

        if write_back_enabled:
            from nexus.services.sync.change_log_store import ChangeLogStore
            from nexus.services.sync.conflict_resolution import ConflictStrategy
            from nexus.services.sync.sync_backlog_store import SyncBacklogStore
            from nexus.services.sync.write_back_service import WriteBackService

            wb_event_bus = svc.event_bus
            if wb_event_bus:
                change_log_store = ChangeLogStore(
                    record_store=gw.record_store, is_postgresql=_is_pg
                )

                # Map env var to ConflictStrategy (backward compat)
                _policy_map = {
                    "lww": ConflictStrategy.KEEP_NEWER,
                    "fork": ConflictStrategy.RENAME_CONFLICT,
                }
                raw_policy = os.getenv("NEXUS_CONFLICT_POLICY", "keep_newer")
                try:
                    default_strategy = ConflictStrategy(raw_policy)
                except ValueError:
                    default_strategy = _policy_map.get(raw_policy, ConflictStrategy.KEEP_NEWER)

                # DT_PIPE wakeup callback (Issue #3194): sync, best-effort, never blocks.
                # SyncBacklogStore calls this after every enqueue() commit to wake the
                # poll loop via pipe signal instead of waiting up to 30s.
                _pm = svc.pipe_manager
                _on_enqueue = None
                if _pm is not None:
                    import contextlib

                    from nexus.system_services.sync.write_back_service import (
                        _BACKLOG_WAKEUP_PIPE,
                    )

                    def _make_on_enqueue(pm: "Any", pipe_path: str) -> "Callable[[], None]":
                        def _signal() -> None:
                            with contextlib.suppress(Exception):
                                pm.pipe_write_nowait(pipe_path, b"\x01")

                        return _signal

                    _on_enqueue = _make_on_enqueue(_pm, _BACKLOG_WAKEUP_PIPE)

                backlog_store = SyncBacklogStore(
                    record_store=gw.record_store,
                    is_postgresql=_is_pg,
                    on_enqueue=_on_enqueue,
                )

                app.state.write_back_service = WriteBackService(
                    gateway=gw,
                    event_bus=wb_event_bus,
                    backlog_store=backlog_store,
                    change_log_store=change_log_store,
                    default_strategy=default_strategy,
                    conflict_log_store=conflict_log_store,
                    pipe_manager=_pm,
                )
                await app.state.write_back_service.start()

                # Register as VFSObserver (Issue #3194): receive file mutation events
                # directly from KernelDispatch OBSERVE phase (us latency, replaces
                # EventBus _subscribe_loop).
                _dispatch = getattr(svc.nexus_fs, "_dispatch", None)
                if _dispatch is not None:
                    _dispatch.register_observe(app.state.write_back_service)
                    logger.debug("WriteBack observer registered with KernelDispatch")

                logger.info("WriteBack service started for bidirectional sync")
                return

        # Fallback: provide InMemoryWriteBack so sync endpoints return
        # zero-change responses instead of 503 in standalone mode.
        from nexus.contracts.protocols.write_back import InMemoryWriteBack

        app.state.write_back_service = InMemoryWriteBack()
        await app.state.write_back_service.start()
        logger.info("WriteBack service started (in-memory fallback)")
    except Exception as e:
        logger.warning("Failed to start WriteBack service: %s", e)


async def _startup_connector_sync(app: "FastAPI", svc: "LifespanServices") -> None:
    """Start ConnectorSyncLoop for periodic background sync (Issue #3148)."""
    if not svc.nexus_fs:
        return

    sync_loop = svc.nexus_fs.service("connector_sync_loop")
    if sync_loop is not None:
        try:
            await sync_loop.start()
            app.state.connector_sync_loop = sync_loop
        except Exception as e:
            logger.warning("Failed to start connector sync loop: %s", e)


async def _startup_lock_manager(_app: "FastAPI", svc: "LifespanServices") -> None:
    """Connect Lock Manager coordination client (Issue #1186)."""
    if not svc.nexus_fs:
        return

    coord_client = svc.coordination_client
    if coord_client is not None:
        try:
            await coord_client.connect()
            logger.info("Lock manager coordination client connected")
        except Exception as e:
            logger.warning("Failed to connect lock manager coordination client: %s", e)


def _startup_exporter_registry(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Initialize ExporterRegistry and configured exporters (Issue #1138)."""
    app.state.exporter_registry = None

    enabled = os.getenv("NEXUS_EVENT_STREAM_ENABLED", "").lower() in ("true", "1", "yes")
    if not enabled:
        logger.debug("Event stream export disabled (NEXUS_EVENT_STREAM_ENABLED not set)")
        return

    try:
        from nexus.services.event_log.exporter_registry import ExporterRegistry
        from nexus.services.event_log.exporters.config import EventStreamConfig
        from nexus.services.event_log.exporters.factory import create_exporter

        exporter_type = os.getenv("NEXUS_EVENT_STREAM_EXPORTER", "kafka")
        config = EventStreamConfig(enabled=True, exporter=exporter_type)

        registry = ExporterRegistry()
        exporter = create_exporter(config)
        if exporter is not None:
            registry.register(exporter)

        app.state.exporter_registry = registry
        logger.info(
            "ExporterRegistry initialized (exporters=%s)",
            registry.exporter_names,
        )
    except Exception as e:
        logger.warning("Failed to initialize ExporterRegistry: %s", e)
