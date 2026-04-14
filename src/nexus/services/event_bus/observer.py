"""EventBusObserver — VFSObserver that forwards FileEvents to distributed EventBus.

Registered in KernelDispatch OBSERVE phase. Replaces the direct
``_publish_file_event()`` calls that previously bypassed the observer pattern.

Issue #1701: event_bus is a system-tier service. The observer is constructed
with a direct ``event_bus`` reference at factory time — no late-binding needed.
Tests that need a different bus use ``nx.swap_service("event_bus_observer",
EventBusObserver(event_bus=shared_bus))`` to hot-swap the observer atomically.

Issue #3646: sync on_mutation — fire-and-forget ``bus.publish()`` via
``create_task``. OBSERVE is fire-and-forget by contract; the async network
I/O (Redis/NATS) runs as a background task, not on the OBSERVE critical path.
This enables full Rust OBSERVE dispatch (all observers sync → no Python
asyncio scheduling needed).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from nexus.core.file_events import ALL_FILE_EVENTS

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.core.file_events import FileEvent
    from nexus.services.event_bus.protocol import EventBusProtocol

logger = logging.getLogger(__name__)


class EventBusObserver:
    """Forward kernel FileEvents to the distributed EventBus (Redis/NATS).

    ``on_mutation()`` is sync — fire-and-forget ``bus.publish()`` via
    ``create_task``. OBSERVE contract is fire-and-forget; network I/O
    runs as a background task outside the OBSERVE critical path.

    Constructed with a direct ``event_bus`` reference (Issue #1701).
    Use ``nx.swap_service("event_bus_observer", EventBusObserver(...))``
    to replace the bus at runtime (e.g. in E2E tests).
    """

    event_mask: int = ALL_FILE_EVENTS
    # Sync on_mutation → safe to run inline on caller's path (Issue #3646).
    # The actual network I/O is fire-and-forget via create_task.

    # ── Hook spec (duck-typed) (Issue #1616) ──────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(observers=(self,))

    def __init__(self, event_bus: "EventBusProtocol | None" = None) -> None:
        self._event_bus = event_bus

    def on_mutation(self, event: "FileEvent") -> None:
        if self._event_bus is None:
            return

        bus = self._event_bus
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._publish(bus, event))
        except RuntimeError:
            pass  # no event loop (e.g. test teardown)

    @staticmethod
    async def _publish(bus: "EventBusProtocol", event: "FileEvent") -> None:
        """Background task: publish event to EventBus (Redis/NATS)."""
        try:
            bus_started = getattr(bus, "_started", False)
            if not bus_started:
                await bus.start()
            await bus.publish(event)
        except Exception as exc:
            logger.warning("EventBusObserver failed to publish: %s", exc)
