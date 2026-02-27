"""EventStreamExporterProtocol — interface for external event stream exporters.

Exporters publish FileEvents to external systems (Kafka, NATS, Pub/Sub).
Each exporter implements this protocol and registers with ExporterRegistry.

Issue #1138: Event Stream Export.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus.core.file_events import FileEvent


@runtime_checkable
class EventStreamExporterProtocol(Protocol):
    """Protocol for external event stream exporters.

    Implementations must be async-safe and handle their own connection lifecycle.
    """

    @property
    def name(self) -> str:
        """Unique exporter name (e.g. 'kafka', 'nats-external', 'pubsub')."""
        ...

    async def publish(self, event: FileEvent) -> None:
        """Publish a single event to the external system.

        Raises on failure (caller handles retry/DLQ routing).
        """
        ...

    async def publish_batch(self, events: list[FileEvent]) -> list[str]:
        """Publish a batch of events. Returns event_ids that failed.

        Exporters may internally chunk the batch for optimal throughput.
        An empty return list means all events were published successfully.
        """
        ...

    async def close(self) -> None:
        """Gracefully close connections and flush pending writes."""
        ...

    async def health_check(self) -> bool:
        """Return True if the exporter backend is reachable."""
        ...
