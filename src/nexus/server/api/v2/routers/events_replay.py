"""Event Replay + SSE streaming + Watch API v2 endpoints (Issue #1139, #2056).

Provides:
- GET /api/v2/events         — v1-compat event log query (operation_id cursors)
- GET /api/v2/events/replay  — cursor-based historical event query (seq_number cursors)
- GET /api/v2/events/stream  — SSE real-time event streaming
- GET /api/v2/watch          — long-polling watch for file changes

Both replay and list share the EventReplayService for consistent filtering.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import NexusFileNotFoundError, NexusPermissionError
from nexus.server.dependencies import get_operation_context, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/events", tags=["events-v2"])
watch_router = APIRouter(prefix="/api/v2/watch", tags=["watch"])

# ---- Per-zone SSE connection tracking ----------------------------------------

_sse_connections: dict[str, int] = {}  # zone_id -> active count
_sse_lock = asyncio.Lock()


async def _acquire_sse_slot(zone_id: str, max_per_zone: int) -> bool:
    """Try to acquire an SSE connection slot for a zone."""
    async with _sse_lock:
        current = _sse_connections.get(zone_id, 0)
        if current >= max_per_zone:
            return False
        _sse_connections[zone_id] = current + 1
        return True


async def _release_sse_slot(zone_id: str) -> None:
    """Release an SSE connection slot for a zone."""
    async with _sse_lock:
        current = _sse_connections.get(zone_id, 0)
        if current > 0:
            _sse_connections[zone_id] = current - 1


def _get_replay_service(request: Request) -> Any:
    """Get or create the EventReplayService from app state."""
    service = getattr(request.app.state, "replay_service", None)
    if service is not None:
        return service

    record_store = getattr(request.app.state, "record_store", None)
    if record_store is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    from nexus.services.event_log.replay import EventReplayService

    service = EventReplayService(
        record_store,
        event_signal=getattr(request.app.state, "event_signal", None),
    )
    request.app.state.replay_service = service
    return service


def _get_stream_config(request: Request) -> dict[str, Any]:  # noqa: ARG001
    """Get SSE configuration from app state or env."""
    import os

    return {
        "max_sse_per_zone": int(os.getenv("NEXUS_SSE_MAX_PER_ZONE", "100")),
        "idle_timeout": float(os.getenv("NEXUS_SSE_IDLE_TIMEOUT", "300")),
        "keepalive_s": float(os.getenv("NEXUS_SSE_KEEPALIVE", "15")),
    }


# =============================================================================
# REST replay endpoint
# =============================================================================


@router.get("/replay", tags=["events-v2"])
async def replay_events(
    request: Request,
    zone_id: str | None = Query(None, description="Filter by zone ID"),
    since_revision: int | None = Query(None, description="Events after this sequence number"),
    since_timestamp: datetime | None = Query(None, description="Events after this time (ISO-8601)"),
    event_types: str | None = Query(
        None, description="Comma-separated event types (e.g. write,delete)"
    ),
    path_pattern: str | None = Query(None, description="Path glob pattern (e.g. /workspace/**)"),
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    cursor: str | None = Query(None, description="Cursor from previous response"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Query historical events with cursor-based pagination.

    Cursor is based on sequence_number for stable, gap-free ordering.
    Supports filtering by zone, agent, event type, path pattern, and time.
    """
    service = _get_replay_service(request)

    # Zone scoping: non-admin users are locked to their zone
    is_admin = auth_result.get("is_admin", False)
    auth_zone = auth_result.get("zone_id")
    effective_zone = zone_id
    if not is_admin and auth_zone:
        effective_zone = auth_zone  # force zone scope
    elif effective_zone is None:
        effective_zone = auth_zone

    # Parse event_types from comma-separated string
    parsed_types = None
    if event_types:
        parsed_types = [t.strip() for t in event_types.split(",") if t.strip()]

    try:
        result = service.replay(
            zone_id=effective_zone,
            since_revision=since_revision,
            since_timestamp=since_timestamp,
            event_types=parsed_types,
            path_pattern=path_pattern,
            agent_id=agent_id,
            limit=limit,
            cursor=cursor,
        )
        return {
            "events": [ev.to_dict() for ev in result.events],
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except Exception as e:
        logger.error("Event replay query error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to replay events") from e


# =============================================================================
# SSE streaming endpoint
# =============================================================================


@router.get("/stream", tags=["events-v2"])
async def stream_events(
    request: Request,
    zone_id: str | None = Query(None, description="Filter by zone ID"),
    since_revision: int | None = Query(None, description="Start from this sequence number"),
    since_timestamp: datetime | None = Query(None, description="Start from this time (ISO-8601)"),
    event_types: str | None = Query(
        None, description="Comma-separated event types (e.g. write,delete)"
    ),
    path_pattern: str | None = Query(None, description="Path glob pattern"),
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> StreamingResponse:
    """Server-Sent Events stream of real-time events.

    Streams historical events first (if since_revision/since_timestamp given),
    then polls for new events. Supports Last-Event-ID for resume after disconnect.

    Connection limits: configurable per-zone max (default 100).
    Idle timeout: disconnect after 5 minutes of no new events.
    Keepalive: ping every 15 seconds.
    """
    service = _get_replay_service(request)
    config = _get_stream_config(request)

    # Zone scoping: non-admin users are locked to their zone
    is_admin = auth_result.get("is_admin", False)
    auth_zone = auth_result.get("zone_id")
    effective_zone = zone_id
    if (not is_admin and auth_zone) or effective_zone is None:
        effective_zone = auth_zone
    zone_key = effective_zone or ROOT_ZONE_ID

    # Check Last-Event-ID header for resume
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id is not None and since_revision is None:
        with contextlib.suppress(ValueError):
            since_revision = int(last_event_id)

    # Enforce per-zone connection limit
    if not await _acquire_sse_slot(zone_key, config["max_sse_per_zone"]):
        raise HTTPException(
            status_code=429,
            detail=f"Too many SSE connections for zone {zone_key}",
        )

    parsed_types = None
    if event_types:
        parsed_types = [t.strip() for t in event_types.split(",") if t.strip()]

    async def event_generator() -> AsyncIterator[str]:
        """SSE event generator with keepalive pings.

        Uses an asyncio.Queue to decouple the event stream from keepalive
        timing. A background task pumps events into the queue while the
        main loop pulls with a timeout — on timeout, emit a keepalive ping.
        """
        try:
            keepalive_interval = config["keepalive_s"]
            idle_timeout = config["idle_timeout"]

            queue: asyncio.Queue[Any] = asyncio.Queue()

            async def _pump_events() -> None:
                """Background task: pump events from stream into queue."""
                try:
                    stream = service.stream(
                        zone_id=effective_zone,
                        since_revision=since_revision,
                        since_timestamp=since_timestamp,
                        event_types=parsed_types,
                        path_pattern=path_pattern,
                        agent_id=agent_id,
                        poll_interval=1.0,
                        idle_timeout=idle_timeout,
                    )
                    async for event in stream:
                        await queue.put(event)
                finally:
                    await queue.put(None)  # sentinel

            pump_task = asyncio.create_task(_pump_events())

            # Yield retry field for client auto-reconnect
            yield "retry: 5000\n\n"

            try:
                while True:
                    if await request.is_disconnected():
                        break

                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue

                    if item is None:
                        break  # stream ended

                    event_data = json.dumps(item.to_dict())
                    seq = item.sequence_number or ""
                    yield f"id: {seq}\nevent: event\ndata: {event_data}\n\n"
            finally:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task

        finally:
            await _release_sse_slot(zone_key)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =============================================================================
# V1-compat event list endpoint (#2056 — ported from v1)
# =============================================================================


@router.get("", tags=["events-v2"])
async def list_events(
    request: Request,
    zone_id: str | None = Query(None, description="Filter by zone ID"),
    since: datetime | None = Query(None, description="Events after this time (ISO-8601)"),
    until: datetime | None = Query(None, description="Events before this time (ISO-8601)"),
    path_prefix: str | None = Query(None, description="Filter by path prefix (wildcard *)"),
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    operation_type: str | None = Query(None, description="Filter by operation type"),
    limit: int = Query(50, ge=1, le=1000, description="Maximum results"),
    cursor: str | None = Query(None, description="Cursor from previous response"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Query the operation log / event history (#2056, ported from v1).

    Delegates to ``EventReplayService.list_v1()`` for cursor-based
    pagination, zone/agent/path/time filters.  Uses DESC ordering
    with operation_id cursors.

    For sequence_number-based replay, use ``GET /api/v2/events/replay``.
    """
    service = _get_replay_service(request)

    # Zone scoping: non-admin users are locked to their zone
    is_admin = auth_result.get("is_admin", False)
    auth_zone = auth_result.get("zone_id")
    effective_zone = zone_id
    if (not is_admin and auth_zone) or effective_zone is None:
        effective_zone = auth_zone

    try:
        result = service.list_v1(
            zone_id=effective_zone,
            agent_id=agent_id,
            operation_type=operation_type,
            path_prefix=path_prefix,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
        )

        events = [
            {
                "event_id": ev.event_id,
                "type": ev.type,
                "path": ev.path,
                "new_path": ev.new_path,
                "zone_id": ev.zone_id,
                "agent_id": ev.agent_id,
                "status": ev.status,
                "delivered": ev.delivered,
                "timestamp": ev.timestamp or None,
            }
            for ev in result.events
        ]

        return {
            "events": events,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }

    except Exception as e:
        logger.error("Event log query error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to query events") from e


# =============================================================================
# Long-polling watch endpoint (#2056 — ported from v1)
# =============================================================================


def _get_nexus_fs(request: Request) -> Any:
    """Get NexusFS from app.state, raising 503 if not initialized."""
    fs = getattr(request.app.state, "nexus_fs", None)
    if fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return fs


@watch_router.get("", tags=["watch"])
async def watch_for_changes(
    request: Request,
    path: str = Query("/**/*", description="Path or glob pattern to watch"),
    timeout: float = Query(30.0, ge=0.1, le=300.0, description="Maximum time to wait in seconds"),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Long-polling endpoint to wait for file system changes.

    Returns the first change matching *path* within *timeout* seconds,
    or ``{"changes": [], "timeout": true}`` if no change occurs.
    """
    nexus_fs = _get_nexus_fs(request)
    context = get_operation_context(auth_result)

    try:
        change = await nexus_fs.sys_watch(path, timeout=timeout, context=context)
        if change is None:
            return {"changes": [], "timeout": True}
        return {"changes": [change], "timeout": False}
    except NotImplementedError as e:
        raise HTTPException(
            status_code=501,
            detail=f"Watch not available: {e}. Requires Redis event bus or same-box backend.",
        ) from None
    except NexusFileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Path not found: {path}") from None
    except NexusPermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    except Exception as e:
        logger.error("Watch error for %s: %s", path, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Watch failed") from e
