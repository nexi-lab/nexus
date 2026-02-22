"""EventBusObserver — VFSObserver that forwards FileEvents to distributed EventBus.

Registered in KernelDispatch OBSERVE phase. Replaces the direct
``_publish_file_event()`` calls that previously bypassed the observer pattern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.file_events import FileEvent

logger = logging.getLogger(__name__)


class EventBusObserver:
    """Forward kernel FileEvents to the distributed EventBus (Redis/NATS).

    ``on_mutation()`` is synchronous (called from KernelDispatch.notify);
    async ``event_bus.publish()`` is dispatched via ``fire_and_forget``,
    matching the existing fire-and-forget semantics.
    """

    def __init__(self, event_bus: Any) -> None:
        self._event_bus = event_bus
        self._started = False

    def on_mutation(self, event: FileEvent) -> None:
        from nexus.lib.sync_bridge import fire_and_forget

        try:
            if not self._started:

                async def _start_and_publish() -> None:
                    await self._event_bus.start()
                    self._started = True
                    await self._event_bus.publish(event)

                fire_and_forget(_start_and_publish())
                return

            fire_and_forget(self._event_bus.publish(event))
        except Exception as exc:
            logger.warning("EventBusObserver failed to publish: %s", exc)
