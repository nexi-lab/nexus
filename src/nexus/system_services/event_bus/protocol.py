"""EventBus protocols — abstract interfaces for event bus implementations."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from nexus.system_services.event_bus.types import FileEvent


@runtime_checkable
class PubSubClientProtocol(Protocol):
    """Protocol for Redis-like pub/sub client providers (e.g., DragonflyClient).

    Captures the interface that RedisEventBus needs, decoupling the
    event bus from the concrete cache driver (KERNEL-ARCHITECTURE.md §1).
    """

    @property
    def client(self) -> Any:
        """Underlying async client with pubsub() and publish() methods."""
        ...

    @property
    def url(self) -> str:
        """Connection URL for creating fresh connections."""
        ...

    async def health_check(self) -> bool:
        """Check if the backend is healthy."""
        ...

    async def get_info(self) -> dict[str, Any]:
        """Get backend info/stats."""
        ...


@dataclass
class AckableEvent:
    """Wrapper around FileEvent with acknowledgment semantics.

    Used by durable subscribers (e.g., NATS JetStream pull consumers) to
    explicitly acknowledge or reject event processing. For backends without
    native ack support (e.g., Redis pub/sub), ack/nack are no-ops.
    """

    event: FileEvent
    _ack_fn: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _nack_fn: Callable[[float | None], Awaitable[None]] | None = field(default=None, repr=False)
    _in_progress_fn: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    async def ack(self) -> None:
        """Acknowledge the event (prevent redelivery)."""
        if self._ack_fn:
            await self._ack_fn()

    async def nack(self, delay: float | None = None) -> None:
        """Negative acknowledge (trigger redelivery after optional delay)."""
        if self._nack_fn:
            await self._nack_fn(delay)

    async def in_progress(self) -> None:
        """Signal that processing is ongoing (extend ack deadline)."""
        if self._in_progress_fn:
            await self._in_progress_fn()


@runtime_checkable
class EventBusProtocol(Protocol):
    """Protocol defining the event bus interface.

    This protocol allows different backend implementations:
    - Redis Pub/Sub (default, implemented as RedisEventBus)
    - NATS JetStream (implemented as NatsEventBus)
    - Future: etcd watch, ZooKeeper watchers, P2P gossip

    All implementations must provide these async methods.
    """

    async def start(self) -> None:
        """Start the event bus. Must be called before publish/subscribe."""
        ...

    async def stop(self) -> None:
        """Stop the event bus and clean up resources."""
        ...

    async def publish(self, event: FileEvent) -> int:
        """Publish an event.

        Args:
            event: FileEvent to publish

        Returns:
            Number of subscribers that received the event
        """
        ...

    async def publish_batch(self, events: list[FileEvent]) -> list[int]:
        """Publish a batch of events atomically.

        Args:
            events: List of FileEvent objects to publish

        Returns:
            List of subscriber counts per event

        Implementations may optimize batching (pipeline, transaction, etc).
        """
        ...

    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_version: int | None = None,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            zone_id: Zone ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds
            since_version: If set, skip events with version <= this value

        Returns:
            FileEvent if matched, None on timeout
        """
        ...

    async def health_check(self) -> bool:
        """Check if the event bus is healthy."""
        ...

    def subscribe(self, zone_id: str) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a zone (async generator)."""
        ...

    def subscribe_durable(
        self,
        zone_id: str,
        consumer_name: str,
        deliver_policy: str = "all",
    ) -> AsyncIterator[AckableEvent]:
        """Subscribe with durable consumer semantics.

        Args:
            zone_id: Zone ID to subscribe to
            consumer_name: Durable consumer name (survives reconnects)
            deliver_policy: Delivery policy ("all", "last", "new")

        Yields:
            AckableEvent objects with ack/nack support
        """
        ...
