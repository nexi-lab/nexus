"""InMemoryEventBus — process-local pub/sub for kernel-only and dev mode.

Per data-storage-matrix.md §8 and CacheStore Implementation Status,
EventBusProtocol needs an in-memory fallback for deployments without
Redis/NATS (kernel-only, embedded, dev/test).

Events are broadcast to subscribers via asyncio.Queue per zone.
No persistence, no durability — purely ephemeral pub/sub.
"""

import asyncio
import contextlib
import fnmatch
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from nexus.services.event_bus.base import EventBusBase
from nexus.services.event_bus.decorators import requires_started
from nexus.services.event_bus.protocol import AckableEvent
from nexus.services.event_bus.types import FileEvent

logger = logging.getLogger(__name__)

_STOP = object()


class InMemoryEventBus(EventBusBase):
    """Process-local in-memory event bus.

    Uses asyncio.Queue per subscriber for fan-out delivery.
    No persistence — events are lost on process exit.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._subscribers: dict[str, list[asyncio.Queue[FileEvent | object]]] = defaultdict(list)
        self._sub_lock = asyncio.Lock()
        self._publish_count = 0

    async def _do_start(self) -> None:
        self._subscribers.clear()
        self._publish_count = 0

    async def _do_stop(self) -> None:
        async with self._sub_lock:
            for queues in self._subscribers.values():
                for q in queues:
                    await q.put(_STOP)
            self._subscribers.clear()

    @requires_started
    async def publish(self, event: FileEvent) -> int:
        zone_id = event.zone_id or "__default__"
        receivers = 0
        async with self._sub_lock:
            for q in self._subscribers.get(zone_id, []):
                try:
                    q.put_nowait(event)
                    receivers += 1
                except asyncio.QueueFull:
                    logger.warning(
                        "InMemoryEventBus: subscriber queue full, dropping event %s",
                        event.event_id,
                    )
        self._publish_count += 1
        return receivers

    async def publish_batch(self, events: list[FileEvent]) -> list[int]:
        return [await self.publish(e) for e in events]

    @requires_started
    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_version: int | None = None,
    ) -> FileEvent | None:
        q: asyncio.Queue[FileEvent | object] = asyncio.Queue(maxsize=256)
        async with self._sub_lock:
            self._subscribers[zone_id].append(q)
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                try:
                    item = await asyncio.wait_for(q.get(), timeout=remaining)
                except TimeoutError:
                    return None
                if item is _STOP or not isinstance(item, FileEvent):
                    return None
                if since_version is not None and (item.version or 0) <= since_version:
                    continue
                if fnmatch.fnmatch(item.path, path_pattern):
                    return item
        finally:
            async with self._sub_lock:
                with contextlib.suppress(ValueError):
                    self._subscribers[zone_id].remove(q)

    @requires_started
    async def health_check(self) -> bool:
        return True

    @requires_started
    async def subscribe(self, zone_id: str) -> AsyncIterator[FileEvent]:
        q: asyncio.Queue[FileEvent | object] = asyncio.Queue(maxsize=1024)
        async with self._sub_lock:
            self._subscribers[zone_id].append(q)
        try:
            while True:
                item = await q.get()
                if item is _STOP or not isinstance(item, FileEvent):
                    break
                yield item
        finally:
            async with self._sub_lock:
                with contextlib.suppress(ValueError):
                    self._subscribers[zone_id].remove(q)

    @requires_started
    async def subscribe_durable(
        self,
        zone_id: str,
        consumer_name: str,  # noqa: ARG002
        deliver_policy: str = "all",  # noqa: ARG002
    ) -> AsyncIterator[AckableEvent]:
        async for event in self.subscribe(zone_id):
            yield AckableEvent(event=event)

    async def get_stats(self) -> dict[str, Any]:
        stats = await super().get_stats()
        async with self._sub_lock:
            total_subs = sum(len(qs) for qs in self._subscribers.values())
        stats.update(
            {
                "zones": len(self._subscribers),
                "total_subscribers": total_subs,
                "total_published": self._publish_count,
            }
        )
        return stats
