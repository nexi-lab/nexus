"""EventBusRemoteWatcher — RemoteWatchProtocol via EventBus (NATS/Dragonfly).

Services-tier wrapper for naming clarity. EventBusBase already satisfies
RemoteWatchProtocol — this class makes the role explicit and allows
future extension (e.g. filtered subscriptions, backpressure).

See: Rust kernel stream_observer.rs for kernel-tier event dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.file_events import FileEvent
    from nexus.services.event_bus.protocol import EventBusProtocol


class EventBusRemoteWatcher:
    """RemoteWatchProtocol via EventBus (NATS/Dragonfly).

    Thin wrapper for naming clarity. Use when external pub/sub infra
    is available and desired for distributed event delivery.
    """

    def __init__(self, event_bus: "EventBusProtocol") -> None:
        self._event_bus = event_bus

    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_version: int | None = None,
    ) -> "FileEvent | None":
        """Delegate to EventBus.wait_for_event()."""
        return await self._event_bus.wait_for_event(
            zone_id=zone_id,
            path_pattern=path_pattern,
            timeout=timeout,
            since_version=since_version,
        )
