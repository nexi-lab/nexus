"""EventBusObserver — VFSObserver that forwards FileEvents to distributed EventBus.

Registered in KernelDispatch OBSERVE phase. Replaces the direct
``_publish_file_event()`` calls that previously bypassed the observer pattern.

Late-binding support (Issue #969):
    The observer can be constructed with a *bus_provider* — any object
    whose ``_event_bus`` attribute may be swapped after boot (e.g. the
    NexusFS instance in E2E tests that inject a shared Redis bus after
    factory construction).  When *bus_provider* is given, the event bus
    is resolved at call time via ``bus_provider._event_bus``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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

    Two construction modes:

    1. **Direct**: ``EventBusObserver(event_bus)`` — bus is fixed at init.
    2. **Late-binding**: ``EventBusObserver(event_bus, bus_provider=nx)``
       — ``nx._event_bus`` is resolved on every ``on_mutation`` call,
       so post-construction overrides (e.g. test fixtures injecting a
       shared Redis bus) are picked up automatically.
    """

    # ── HotSwappable protocol (Issue #1616) ────────────────────────────

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(observers=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def __init__(
        self,
        event_bus: "EventBusProtocol | None" = None,
        *,
        bus_provider: Any = None,
    ) -> None:
        self._event_bus = event_bus
        self._bus_provider = bus_provider

    def _resolve_bus(self) -> "EventBusProtocol | None":
        """Return the effective event bus (late-binding aware)."""
        if self._bus_provider is not None:
            return getattr(self._bus_provider, "_event_bus", None)
        return self._event_bus

    def on_mutation(self, event: FileEvent) -> None:
        bus = self._resolve_bus()
        if bus is None:
            return

        from nexus.lib.sync_bridge import fire_and_forget

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
