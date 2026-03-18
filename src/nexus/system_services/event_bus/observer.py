"""EventBusObserver — VFSObserver that forwards FileEvents to distributed EventBus.

Registered in KernelDispatch OBSERVE phase. Replaces the direct
``_publish_file_event()`` calls that previously bypassed the observer pattern.

Issue #1701: event_bus is Tier 1 (SystemServices). The observer is constructed
with a direct ``event_bus`` reference at factory time — no late-binding needed.
Tests that need a different bus use ``await nx.swap_service("event_bus_observer",
EventBusObserver(event_bus=shared_bus))`` to hot-swap the observer atomically.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.core.file_events import FileEvent
    from nexus.system_services.event_bus.protocol import EventBusProtocol

logger = logging.getLogger(__name__)


class EventBusObserver:
    """Forward kernel FileEvents to the distributed EventBus (Redis/NATS).

    ``on_mutation()`` is synchronous (called from KernelDispatch.notify);
    async ``event_bus.publish()`` is dispatched via ``fire_and_forget``,
    matching the existing fire-and-forget semantics.

    Constructed with a direct ``event_bus`` reference (Issue #1701).
    Use ``await nx.swap_service("event_bus_observer", EventBusObserver(...))``
    to replace the bus at runtime (e.g. in E2E tests).
    """

    # ── HotSwappable protocol (Issue #1616) ────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(observers=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def __init__(self, event_bus: "EventBusProtocol | None" = None) -> None:
        self._event_bus = event_bus

    def on_mutation(self, event: "FileEvent") -> None:
        if self._event_bus is None:
            return

        from nexus.lib.sync_bridge import fire_and_forget

        bus = self._event_bus
        try:
            # Check if the bus is already started (e.g. test fixtures pre-start
            # the shared bus, or the server lifespan already started it).
            bus_started = getattr(bus, "_started", False)
            if not bus_started:

                async def _start_and_publish() -> None:
                    await bus.start()
                    await bus.publish(event)

                fire_and_forget(_start_and_publish())
                return

            fire_and_forget(bus.publish(event))
        except Exception as exc:
            logger.warning("EventBusObserver failed to publish: %s", exc)
