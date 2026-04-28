"""WriteObserverProtocol — kernel write-mutation observer interface.

Defines the contract for write observers injected into NexusFS kernel.
The kernel calls on_write()/on_delete()/on_rename()/on_write_batch()/
on_mkdir()/on_rmdir() after Metastore mutations. Observers handle
side-effects (audit trail, version recording, etc.) and own their
own error policy.

The kernel passes kernel-native types only (FileMetadata).  Observers
derive whatever they need (e.g. snapshot_hash = metadata.content_id).

Current implementations:
- RecordStoreWriteObserver (record_store_write_observer): synchronous audit trail + versioning (strict_mode)
- RecordStoreWriteObserver (piped_record_store_write_observer): OBSERVE-phase observer with debounced batch flush

The kernel is a pure caller — it never catches observer exceptions.
Each implementation decides its own failure handling strategy.

Tracked by: #55 (Move _audit_strict_mode from kernel to observer)
Issue #900: Replaced snapshot_hash/metadata_snapshot with metadata.
"""

from typing import Protocol, runtime_checkable

from nexus.contracts.metadata import FileMetadata


@runtime_checkable
class WriteObserverProtocol(Protocol):
    """Protocol for kernel write-mutation observers.

    Duck-typed interface injected into NexusFS as write_observer.
    Implementations handle RecordStore side-effects (audit log,
    version history) and own their error policy.

    The kernel passes ``metadata: FileMetadata`` — observers derive
    ``snapshot_hash`` (``metadata.content_id``) and ``metadata_snapshot``
    (``metadata.to_dict()``) internally.
    """

    def on_write(
        self,
        metadata: FileMetadata,
        *,
        is_new: bool,
        path: str,
        old_metadata: FileMetadata | None = ...,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
    ) -> None:
        """Called after a single file write completes in Metastore.

        Args:
            metadata: New file metadata after the write.
            is_new: True if this is a new file (no previous version).
            path: Virtual path of the file.
            old_metadata: Previous metadata before write (for undo).
                          None when is_new=True.
        """
        ...

    def on_write_batch(
        self,
        items: list[tuple[FileMetadata, bool]],
        *,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
        urgency: str | None = ...,
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
        metadata: FileMetadata | None = ...,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
    ) -> None:
        """Called after a file rename completes in Metastore."""
        ...

    def on_delete(
        self,
        path: str,
        *,
        metadata: FileMetadata | None = ...,
        zone_id: str | None = ...,
        agent_id: str | None = ...,
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
