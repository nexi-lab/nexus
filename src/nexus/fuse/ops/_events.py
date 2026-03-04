"""FUSE event dispatcher for fire-and-forget event delivery.

Extracted from NexusFUSEOperations to be independently testable
and composable via FUSESharedContext.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

# Import event system (Issue #1115)
try:
    from nexus.core.file_events import FileEvent, FileEventType

    HAS_EVENT_BUS = True
except ImportError:
    HAS_EVENT_BUS = False
    FileEvent = None  # type: ignore[misc,assignment]
    FileEventType = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


class FUSEEventDispatcher:
    """Dispatches file events to downstream systems (non-blocking).

    Events are delivered to:
    - RedisEventBus (distributed cache invalidation)
    - SubscriptionManager (webhook delivery)
    - TriggerManager (workflow triggers)
    """

    def __init__(
        self,
        event_bus: Any | None,
        subscription_manager: Any | None,
        zone_id_fn: Callable[[], str | None],
        enable_events: bool,
        event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._subscription_manager = subscription_manager
        self._zone_id_fn = zone_id_fn
        self._enable_events = enable_events
        self._event_loop = event_loop

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop for async event dispatching."""
        self._event_loop = loop

    def fire(
        self,
        event_type: Any,
        path: str,
        old_path: str | None = None,
        size: int | None = None,
    ) -> None:
        """Fire an event to downstream systems (non-blocking)."""
        if not self._enable_events or not HAS_EVENT_BUS:
            return

        if FileEvent is None:
            return

        try:
            event = FileEvent(
                type=event_type,
                path=path,
                zone_id=self._zone_id_fn(),
                old_path=old_path,
                size=size,
            )

            if self._event_loop is not None and self._event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._dispatch(event),
                    self._event_loop,
                )
            else:
                logger.debug("[FUSE-EVENT] No event loop, skipping: %s %s", event_type, path)

        except Exception as e:
            logger.debug("[FUSE-EVENT] Failed to fire event: %s", e)

    async def _dispatch(self, event: Any) -> None:
        """Dispatch event to all downstream systems."""
        try:
            if self._event_bus is not None:
                try:
                    await self._event_bus.publish(event)
                except Exception as e:
                    logger.debug("[FUSE-EVENT] Event bus publish failed: %s", e)

            try:
                sub_manager = self._subscription_manager
                if sub_manager is not None:
                    event_type_str = (
                        event.type.value if hasattr(event.type, "value") else str(event.type)
                    )
                    await sub_manager.broadcast(
                        event_type=event_type_str,
                        data={
                            "file_path": event.path,
                            "old_path": event.old_path,
                            "size": event.size,
                            "timestamp": event.timestamp,
                        },
                        zone_id=event.zone_id or ROOT_ZONE_ID,
                    )
            except Exception as e:
                logger.debug("[FUSE-EVENT] Webhook broadcast failed: %s", e)

            logger.debug("[FUSE-EVENT] Dispatched: %s %s", event.type, event.path)

        except Exception as e:
            logger.debug("[FUSE-EVENT] Dispatch failed: %s", e)
