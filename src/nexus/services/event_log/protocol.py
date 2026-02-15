"""EventLogProtocol — durable event persistence interface.

Defines the contract for event log backends. This is a service-layer protocol,
NOT a kernel pillar ABC. It serves as the durability backend for EventBus.

Current implementations:
- WALEventLog: Rust-backed WAL (sub-5μs writes, local disk)

Note: PGEventLog was removed in Issue #1241.  Event delivery from
``operation_log`` is now handled by ``EventDeliveryWorker`` (outbox).

Tracked by: #1397 (Rust-Accelerated Event Log WAL)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.event_bus import FileEvent


@dataclass(frozen=True)
class EventLogConfig:
    """Configuration for event log backends.

    Attributes:
        wal_dir: Directory for WAL segment files.
        segment_size_bytes: Max segment file size before rotation (default 4MB).
        sync_mode: "every" = fsync per write (durable), "none" = OS-buffered (fast).
    """

    wal_dir: Path = field(default_factory=lambda: Path(".nexus-data/wal"))
    segment_size_bytes: int = 4 * 1024 * 1024  # 4 MB
    sync_mode: Literal["every", "none"] = "every"


@runtime_checkable
class EventLogProtocol(Protocol):
    """Protocol for durable event log backends.

    Provides ordered, zone-aware event persistence with crash recovery.
    All sequence numbers are monotonically increasing across appends.
    """

    async def append(self, event: FileEvent) -> int:
        """Append a single event to the log.

        Returns:
            Sequence number assigned to the event.
        """
        ...

    async def append_batch(self, events: list[FileEvent]) -> list[int]:
        """Append multiple events atomically.

        Returns:
            List of sequence numbers, one per event.
        """
        ...

    async def read_from(
        self,
        seq: int,
        limit: int = 1000,
        *,
        zone_id: str | None = None,
    ) -> list[FileEvent]:
        """Read events starting from a sequence number.

        Args:
            seq: Start reading from this sequence number (inclusive).
            limit: Maximum number of events to return.
            zone_id: If set, only return events from this zone.

        Returns:
            List of events ordered by sequence number.
        """
        ...

    async def truncate(self, before_seq: int) -> int:
        """Remove events with sequence numbers < before_seq.

        Returns:
            Number of records truncated.
        """
        ...

    async def sync(self) -> None:
        """Force fsync of the active segment to disk."""
        ...

    async def close(self) -> None:
        """Flush and close all segment files."""
        ...

    def current_sequence(self) -> int:
        """Return the most recently assigned sequence number (0 if empty)."""
        ...

    async def health_check(self) -> bool:
        """Return True if the log is open and writable."""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None: ...
