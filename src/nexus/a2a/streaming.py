"""A2A SSE streaming handlers.

Extracted from ``router.py`` for testability and separation of
concerns (Decision 1 / #1585).  Contains the SSE event loop,
streaming send/subscribe handlers, and SSE response factory.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from nexus.a2a.exceptions import InvalidParamsError
from nexus.a2a.models import TERMINAL_STATES, SendParams, TaskIdParams
from nexus.a2a.stream_registry import StreamRegistry
from nexus.a2a.task_manager import TaskManager

logger = logging.getLogger(__name__)

# SSE configuration
SSE_KEEPALIVE_INTERVAL = 15  # seconds
SSE_MAX_LIFETIME = 1800  # 30 minutes

async def sse_event_loop(
    task: Any,
    queue: asyncio.Queue[dict[str, Any] | None],
    stream_registry: StreamRegistry,
    task_id: str,
) -> AsyncGenerator[str, None]:
    """Shared SSE event loop with keepalive and max lifetime.

    Used by both ``sendStreamingMessage`` and ``subscribeToTask``.
    """
    try:
        # First event: minimal task state (id + status + contextId only) — Decision 15
        initial = {
            "task": {
                "id": task.id,
                "status": task.status.model_dump(mode="json"),
                "contextId": task.contextId,
            }
        }
        yield format_sse_event(initial)

        # If already terminal, stop
        if task.status.state in TERMINAL_STATES:
            return

        start_time = time.monotonic()
        while True:
            if time.monotonic() - start_time > SSE_MAX_LIFETIME:
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_INTERVAL)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                break
            yield format_sse_event(event)
            status_update = event.get("statusUpdate")
            if status_update and status_update.get("final"):
                break
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        stream_registry.unregister(task_id, queue)

def _sse_response(
    task: Any,
    queue: asyncio.Queue[dict[str, Any] | None],
    stream_registry: StreamRegistry,
) -> StreamingResponse:
    """Create a standard SSE StreamingResponse.

    DRY factory (Decision 6) — used by both send-streaming and subscribe.
    """
    return StreamingResponse(
        sse_event_loop(task, queue, stream_registry, task.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

async def handle_streaming(
    *,
    method: str,
    params: dict[str, Any],
    request_id: str | int,
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
    stream_registry: StreamRegistry,
) -> StreamingResponse:
    """Handle streaming methods — returns an SSE StreamingResponse."""

    if method == "a2a.tasks.sendStreamingMessage":
        return await handle_send_streaming(
            params, request_id, zone_id, agent_id, task_manager, stream_registry
        )
    elif method == "a2a.tasks.subscribeToTask":
        return await handle_subscribe(params, request_id, zone_id, task_manager, stream_registry)
    else:
        raise InvalidParamsError(data={"method": method})

async def handle_send_streaming(
    params: dict[str, Any],
    _request_id: str | int,
    zone_id: str,
    agent_id: str | None,
    task_manager: TaskManager,
    stream_registry: StreamRegistry,
) -> StreamingResponse:
    """Handle ``a2a.tasks.sendStreamingMessage`` — SSE response."""
    try:
        send_params = SendParams.model_validate(params)
    except ValidationError as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    task = await task_manager.create_task(
        send_params.message,
        zone_id=zone_id,
        agent_id=agent_id,
        metadata=send_params.metadata,
    )
    queue = stream_registry.register(task.id)
    return _sse_response(task, queue, stream_registry)

async def handle_subscribe(
    params: dict[str, Any],
    _request_id: str | int,
    zone_id: str,
    task_manager: TaskManager,
    stream_registry: StreamRegistry,
) -> StreamingResponse:
    """Handle ``a2a.tasks.subscribeToTask`` — SSE response for existing task."""
    try:
        subscribe_params = TaskIdParams.model_validate(params)
    except ValidationError as e:
        raise InvalidParamsError(data={"detail": str(e)}) from e

    task = await task_manager.get_task(subscribe_params.taskId, zone_id=zone_id)
    queue = stream_registry.register(task.id)
    return _sse_response(task, queue, stream_registry)

def format_sse_event(data: dict[str, Any]) -> str:
    """Format a dict as an SSE event string."""
    payload = json.dumps(data, default=str)
    return f"data: {payload}\n\n"
