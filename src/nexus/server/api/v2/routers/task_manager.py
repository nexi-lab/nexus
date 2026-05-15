"""Task Manager REST API — SSE-only endpoints.

CRUD/query endpoints have been migrated to RPC services.
Only the SSE streaming endpoint remains (requires HTTP keep-alive).

- GET /api/v2/tasks/events  — SSE stream of task mutations
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task_manager"])

# =============================================================================
# SSE via DT_STREAM (task change notifications)
# =============================================================================

_TASK_SSE_STREAM_PATH = "/nexus/streams/task-events"


def _get_stream_manager(request: Request) -> Any:
    """Get StreamManager from app state (optional — SSE degrades gracefully)."""
    return getattr(request.app.state, "task_stream_manager", None)


@router.get("/api/v2/tasks/events")
async def task_events(request: Request) -> StreamingResponse:
    """SSE stream of task mutation notifications via DT_STREAM.

    Each SSE client maintains its own byte offset into the stream,
    providing true fan-out without destructive reads (unlike DT_PIPE).
    """
    sm = _get_stream_manager(request)

    async def _stream() -> AsyncGenerator[str, None]:
        if sm is None:
            yield ": stream manager not available\n\n"
            return

        offset = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                data, next_offset = sm.stream_read_at(_TASK_SSE_STREAM_PATH, offset)
                offset = next_offset
                yield f"data: {data.decode()}\n\n"
            except Exception:
                # StreamEmptyError or StreamNotFoundError — wait and send keepalive
                await asyncio.sleep(25)
                yield ": keepalive\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
