"""WriteObserverProtocol — kernel write-mutation observer interface.

Defines the contract for write observers injected into NexusFS kernel.
The kernel calls observer methods directly via typed Protocol dispatch.
Observers own their error policy (#55). The kernel has a defensive
safety net (_handle_observer_error) for any errors that escape.

Current implementations:
- RecordStoreWriteObserver: synchronous audit trail + versioning (strict_mode)
- BufferedRecordStoreWriteObserver: async via WriteBuffer (fire-and-forget)

Migrated from getattr() dispatch in #1631.
Tracked by: #55 (Move _audit_strict_mode from kernel to observer)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.metadata import FileMetadata


@runtime_checkable
class WriteObserverProtocol(Protocol):
    """Protocol for kernel write-mutation observers.

    Duck-typed interface injected into NexusFS as write_observer.
    Implementations handle RecordStore side-effects (audit log,
    version history) and own their error policy.
    """

    def on_write(
        self,
        metadata: FileMetadata,
        *,
        is_new: bool,
        path: str,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
        snapshot_hash: str | None = ...,
        metadata_snapshot: dict[str, Any] | None = ...,
        urgency: str | None = ...,
    ) -> None:
        """Called after a single file write completes in Metastore."""
        ...

    def on_write_batch(
        self,
        items: list[tuple[FileMetadata, bool]],
        *,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
    ) -> None:
        """Called after a batch write completes in Metastore.

        Args:
            items: List of (metadata, is_new) tuples.
        """
        ...

    def on_rename(
        self,
        old_path: str,
        new_path: str,
        *,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
        snapshot_hash: str | None = ...,
        metadata_snapshot: dict[str, Any] | None = ...,
    ) -> None:
        """Called after a file rename completes in Metastore."""
        ...

    def on_delete(
        self,
        path: str,
        *,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
        snapshot_hash: str | None = ...,
        metadata_snapshot: dict[str, Any] | None = ...,
    ) -> None:
        """Called after a file delete completes in Metastore."""
        ...

    def on_mkdir(
        self,
        path: str,
        *,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
    ) -> None:
        """Called after a directory creation completes in Metastore."""
        ...

    def on_rmdir(
        self,
        path: str,
        *,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
        recursive: bool = ...,
    ) -> None:
        """Called after a directory removal completes in Metastore."""
        ...
