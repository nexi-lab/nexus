"""Event Replay + SSE streaming API v2 endpoints (Issue #1139).

Provides:
- GET /api/v2/events/replay  — cursor-based historical event query
- GET /api/v2/events/stream  — SSE real-time event streaming

Both share the EventReplayService for consistent filtering and pagination.
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

from nexus.server.dependencies import get_auth_result

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/events", tags=["events-v2"])

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

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None or not callable(session_factory):
        raise HTTPException(status_code=503, detail="Database not configured")

    from nexus.services.event_log.replay_service import EventReplayService

    service = EventReplayService(session_factory)
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
    _auth_result: dict[str, Any] | None = Depends(get_auth_result),
) -> dict[str, Any]:
    """Query historical events with cursor-based pagination.

    Cursor is based on sequence_number for stable, gap-free ordering.
    Supports filtering by zone, agent, event type, path pattern, and time.
    """
    service = _get_replay_service(request)

    # Default zone from auth
    effective_zone = zone_id
    if effective_zone is None and _auth_result:
        effective_zone = _auth_result.get("zone_id")

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
    _auth_result: dict[str, Any] | None = Depends(get_auth_result),
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

    # Default zone from auth
    effective_zone = zone_id
    if effective_zone is None and _auth_result:
        effective_zone = _auth_result.get("zone_id")
    zone_key = effective_zone or "default"

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
        """SSE event generator with keepalive pings."""
        try:
            keepalive_interval = config["keepalive_s"]
            idle_timeout = config["idle_timeout"]

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

            last_keepalive = asyncio.get_event_loop().time()

            # Yield retry field for client auto-reconnect
            yield "retry: 5000\n\n"

            async for event in stream:
                if await request.is_disconnected():
                    break

                event_data = json.dumps(event.to_dict())
                seq = event.sequence_number or ""
                yield f"id: {seq}\nevent: event\ndata: {event_data}\n\n"
                last_keepalive = asyncio.get_event_loop().time()

            # Check if we need a keepalive ping
            now = asyncio.get_event_loop().time()
            if now - last_keepalive >= keepalive_interval:
                yield ": keepalive\n\n"

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
