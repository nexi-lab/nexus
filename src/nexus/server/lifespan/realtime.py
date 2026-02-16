"""Realtime startup/shutdown: event bus, event log, WebSocket, WriteBack, locks.

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_realtime(app: FastAPI) -> list[asyncio.Task]:
    """Initialize realtime infrastructure and return background tasks.

    Covers:
    - Event Log WAL (Issue #1397)
    - Event bus start + wiring
    - WebSocket Manager (Issue #1116)
    - WriteBack Service (Issue #1129)
    - Lock Manager coordination (Issue #1186)
    """
    bg_tasks: list[asyncio.Task] = []

    _startup_event_log(app)
    await _startup_event_bus(app)
    await _startup_websocket(app)
    await _startup_writeback(app)
    await _startup_lock_manager(app)

    return bg_tasks


async def shutdown_realtime(app: FastAPI) -> None:
    """Shutdown realtime infrastructure in reverse order."""
    # Disconnect Lock Manager coordination client (Issue #1186)
    if app.state.nexus_fs and hasattr(app.state.nexus_fs, "_coordination_client"):
        coord_client = app.state.nexus_fs._coordination_client
        if coord_client is not None:
            try:
                await coord_client.disconnect()
                logger.info("Lock manager coordination client disconnected")
            except Exception as e:
                logger.debug(f"Error disconnecting coordination client: {e}")

    # Shutdown WebSocket manager (Issue #1116)
    if app.state.websocket_manager:
        try:
            await app.state.websocket_manager.stop()
            logger.info("WebSocket manager stopped")
        except Exception as e:
            logger.warning(f"Error shutting down WebSocket manager: {e}")

    # Stop WriteBack Service (Issue #1129)
    if app.state.write_back_service:
        try:
            await app.state.write_back_service.stop()
            logger.info("WriteBack service stopped")
        except Exception as e:
            logger.warning(f"Error shutting down WriteBack service: {e}")

    # Stop event bus (Issue #1331)
    _ebus = getattr(app.state.nexus_fs, "_event_bus", None) if app.state.nexus_fs else None
    if _ebus is not None:
        try:
            await _ebus.stop()
            logger.info("Event bus stopped")
        except Exception as e:
            logger.warning(f"Error shutting down event bus: {e}")

    # Close Event Log WAL (Issue #1397)
    if app.state.event_log:
        try:
            await app.state.event_log.close()
            logger.info("Event log closed")
        except Exception as e:
            logger.warning(f"Error closing event log: {e}")

    # Close subscription manager
    if app.state.subscription_manager:
        await app.state.subscription_manager.close()
        from nexus.server.subscriptions import set_subscription_manager

        set_subscription_manager(None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _startup_event_log(app: FastAPI) -> None:
    """Event Log WAL for durable event persistence (Issue #1397)."""
    app.state.event_log = None
    try:
        from nexus.services.event_log import EventLogConfig, create_event_log

        wal_dir = os.getenv("NEXUS_WAL_DIR", ".nexus-data/wal")
        sync_mode = os.getenv("NEXUS_WAL_SYNC_MODE", "every")
        segment_size = int(os.getenv("NEXUS_WAL_SEGMENT_SIZE", str(4 * 1024 * 1024)))

        event_log_config = EventLogConfig(
            wal_dir=Path(wal_dir),
            segment_size_bytes=segment_size,
            sync_mode=sync_mode,  # type: ignore[arg-type]
        )
        app.state.event_log = create_event_log(
            event_log_config,
            session_factory=getattr(app.state, "session_factory", None),
        )
        if app.state.event_log:
            logger.info(f"Event log initialized (wal_dir={wal_dir}, sync_mode={sync_mode})")
    except Exception as e:
        logger.warning(f"Failed to initialize event log: {e}")


async def _startup_event_bus(app: FastAPI) -> None:
    """Start event bus and wire event log for WAL-first persistence (Issue #1397)."""
    if not app.state.nexus_fs:
        return

    event_bus_ref = getattr(app.state.nexus_fs, "_event_bus", None)
    if event_bus_ref is None:
        return

    # Connect the underlying DragonflyClient (async init required)
    redis_client = getattr(event_bus_ref, "_redis", None)
    if redis_client and hasattr(redis_client, "connect"):
        try:
            await redis_client.connect()
        except Exception as e:
            logger.warning(f"Failed to connect event bus Redis client: {e}")

    # Start the event bus
    if hasattr(event_bus_ref, "start") and not getattr(event_bus_ref, "_started", True):
        try:
            await event_bus_ref.start()
            logger.info("Event bus started for event publishing")
        except Exception as e:
            logger.warning(f"Failed to start event bus: {e}")

    # Issue #1331: Store main event loop ref for cross-thread event publishing
    app.state.nexus_fs._main_event_loop = asyncio.get_running_loop()

    # Wire event_log into EventBus for WAL-first durability (Issue #1397)
    if app.state.event_log is not None:
        event_bus_ref._event_log = app.state.event_log
        logger.info("Event log wired into EventBus (WAL-first before pub/sub)")


async def _startup_websocket(app: FastAPI) -> None:
    """Initialize WebSocket Manager for real-time events (Issue #1116)."""
    try:
        from nexus.server.websocket import WebSocketManager

        event_bus = None
        if app.state.nexus_fs and hasattr(app.state.nexus_fs, "_event_bus"):
            event_bus = app.state.nexus_fs._event_bus

        app.state.websocket_manager = WebSocketManager(
            event_bus=event_bus,
            reactive_manager=app.state.reactive_subscription_manager,
        )
        await app.state.websocket_manager.start()
        logger.info("WebSocket manager started for real-time events")
    except Exception as e:
        logger.warning(f"Failed to start WebSocket manager: {e}")


async def _startup_writeback(app: FastAPI) -> None:
    """Initialize WriteBack Service for bidirectional sync (Issue #1129/#1130)."""
    write_back_enabled = os.getenv("NEXUS_WRITE_BACK", "").lower() in ("true", "1", "yes")
    if not (write_back_enabled and app.state.nexus_fs):
        return

    try:
        from nexus.services.change_log_store import ChangeLogStore
        from nexus.services.conflict_log_store import ConflictLogStore
        from nexus.services.conflict_resolution import ConflictStrategy
        from nexus.services.gateway import NexusFSGateway
        from nexus.services.sync_backlog_store import SyncBacklogStore
        from nexus.services.write_back_service import WriteBackService

        gw = NexusFSGateway(app.state.nexus_fs)

        # ConflictLogStore is always available for the REST API
        conflict_log_store = ConflictLogStore(gw)
        app.state.conflict_log_store = conflict_log_store

        wb_event_bus = None
        if hasattr(app.state.nexus_fs, "_event_bus"):
            wb_event_bus = app.state.nexus_fs._event_bus
        if wb_event_bus:
            backlog_store = SyncBacklogStore(gw)
            change_log_store = ChangeLogStore(gw)

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

            app.state.write_back_service = WriteBackService(
                gateway=gw,
                event_bus=wb_event_bus,
                backlog_store=backlog_store,
                change_log_store=change_log_store,
                default_strategy=default_strategy,
                conflict_log_store=conflict_log_store,
            )
            await app.state.write_back_service.start()
            logger.info("WriteBack service started for bidirectional sync")
        else:
            logger.debug("WriteBack service skipped: no event bus available")
    except Exception as e:
        logger.warning(f"Failed to start WriteBack service: {e}")


async def _startup_lock_manager(app: FastAPI) -> None:
    """Connect Lock Manager coordination client (Issue #1186)."""
    if not (app.state.nexus_fs and hasattr(app.state.nexus_fs, "_coordination_client")):
        return

    coord_client = app.state.nexus_fs._coordination_client
    if coord_client is not None:
        try:
            await coord_client.connect()
            logger.info("Lock manager coordination client connected")
        except Exception as e:
            logger.warning(f"Failed to connect lock manager coordination client: {e}")
