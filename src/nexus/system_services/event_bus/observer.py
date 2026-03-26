"""EventBusObserver — VFSObserver that forwards FileEvents to distributed EventBus.

Registered in KernelDispatch OBSERVE phase. Replaces the direct
``_publish_file_event()`` calls that previously bypassed the observer pattern.

Issue #1701: event_bus is Tier 1 (SystemServices). The observer is constructed
with a direct ``event_bus`` reference at factory time — no late-binding needed.
Tests that need a different bus use ``await nx.swap_service("event_bus_observer",
EventBusObserver(event_bus=shared_bus))`` to hot-swap the observer atomically.

Issue #1812: async on_mutation — directly awaits bus.publish() instead of
fire_and_forget, since KernelDispatch.notify() now dispatches observers as
a single asyncio.Task via gather().
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.core.file_events import ALL_FILE_EVENTS

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.core.file_events import FileEvent
    from nexus.system_services.event_bus.protocol import EventBusProtocol

logger = logging.getLogger(__name__)


class EventBusObserver:
    """Forward kernel FileEvents to the distributed EventBus (Redis/NATS).

    ``on_mutation()`` is async — awaits ``bus.publish()`` directly.
    KernelDispatch.notify() dispatches all observers concurrently via
    ``gather()`` in a single ``create_task``.

    Constructed with a direct ``event_bus`` reference (Issue #1701).
    Use ``await nx.swap_service("event_bus_observer", EventBusObserver(...))``
    to replace the bus at runtime (e.g. in E2E tests).
    """

    event_mask: int = ALL_FILE_EVENTS

    # ── Hook spec (duck-typed) (Issue #1616) ──────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(observers=(self,))

    def __init__(self, event_bus: "EventBusProtocol | None" = None) -> None:
        self._event_bus = event_bus

    async def on_mutation(self, event: "FileEvent") -> None:
        if self._event_bus is None:
            return

        bus = self._event_bus
        try:
            bus_started = getattr(bus, "_started", False)
            if not bus_started:
                await bus.start()
            await bus.publish(event)
        except Exception as exc:
            logger.warning("EventBusObserver failed to publish: %s", exc)
