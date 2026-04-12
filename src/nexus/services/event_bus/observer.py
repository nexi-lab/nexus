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

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.services.event_bus.protocol import EventBusProtocol

logger = logging.getLogger(__name__)


class EventBusObserver:
    """Wrapper for the distributed EventBus (Redis/NATS).

    Formerly a VFSObserver (on_mutation via Rust dispatch_observers).
    Now the Rust kernel dispatches observers directly. This class is
    retained as a service-registry entry for event_bus lifecycle and
    for explicit publish calls from factory wiring.

    Constructed with a direct ``event_bus`` reference (Issue #1701).
    Use ``nx.swap_service("event_bus_observer", EventBusObserver(...))``
    to replace the bus at runtime (e.g. in E2E tests).
    """

    def __init__(self, event_bus: "EventBusProtocol | None" = None) -> None:
        self._event_bus = event_bus
