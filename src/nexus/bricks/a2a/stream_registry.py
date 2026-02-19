"""SSE stream registry for A2A tasks.

Manages active Server-Sent-Event (SSE) stream subscriptions.  Each task
can have multiple subscribers; events are pushed to all of them.

Extracted from ``TaskManager`` following the Single Responsibility
Principle (mirrors the HeartbeatBuffer extraction pattern).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress as _suppress
from typing import Any

logger = logging.getLogger(__name__)


class StreamRegistry:
    """Registry of active SSE stream queues per task.

    Parameters
    ----------
    maxsize:
        Maximum number of events each subscriber queue can hold.
        When a queue is full the event is dropped and a warning is
        logged (prevents unbounded memory growth).
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._maxsize = maxsize
        self._active_streams: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}

    def register(self, task_id: str) -> asyncio.Queue[dict[str, Any] | None]:
        """Register a new SSE stream for a task.

        Returns a bounded ``asyncio.Queue`` that will receive stream
        events.  ``None`` is pushed as a sentinel to signal stream
        closure.
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=self._maxsize)
        self._active_streams.setdefault(task_id, []).append(queue)
        return queue

    def unregister(self, task_id: str, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        """Remove an SSE stream registration."""
        streams = self._active_streams.get(task_id)
        if streams is None:
            return
        with _suppress(ValueError):
            streams.remove(queue)
        if not streams:
            del self._active_streams[task_id]

    def push_event(self, task_id: str, event: dict[str, Any]) -> None:
        """Push an event to all active streams for a task.

        Events are delivered via ``put_nowait``.  If a subscriber queue
        is full the event is dropped and a warning is logged.
        """
        streams = self._active_streams.get(task_id)
        if not streams:
            return
        for queue in list(streams):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("SSE queue full for task %s, dropping event", task_id)
