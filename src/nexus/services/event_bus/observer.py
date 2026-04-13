"""EventBusObserver — forwards FileEvents to distributed EventBus.

Registered as a service-registry entry for event_bus lifecycle and
explicit publish calls from factory wiring.

Issue #1701: event_bus is a system-tier service. The observer is constructed
with a direct ``event_bus`` reference at factory time — no late-binding needed.
Tests that need a different bus use ``nx.swap_service("event_bus_observer",
EventBusObserver(event_bus=shared_bus))`` to hot-swap the observer atomically.

Issue #3646: observer dispatch is now fully Rust-native. This class is
retained for service lifecycle and explicit publish() calls, not for
on_mutation dispatch.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.services.event_bus.protocol import EventBusProtocol

logger = logging.getLogger(__name__)


class EventBusObserver:
    """Wrapper for the distributed EventBus (Redis/NATS).

    Retained as a service-registry entry for event_bus lifecycle and
    for explicit publish calls from factory wiring. Observer dispatch
    is handled by the Rust kernel's MutationObserver trait.

    Constructed with a direct ``event_bus`` reference (Issue #1701).
    Use ``nx.swap_service("event_bus_observer", EventBusObserver(...))``
    to replace the bus at runtime (e.g. in E2E tests).
    """

    def __init__(self, event_bus: "EventBusProtocol | None" = None) -> None:
        self._event_bus = event_bus
