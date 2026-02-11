"""RecordStore write-through syncer.

Bundles OperationLogger + VersionRecorder into a single injectable observer.
Created by factory.py, injected into NexusFS kernel as write_observer.

The kernel calls on_write()/on_delete() after Metastore mutations.
This syncer handles all RecordStore side-effects in a single transaction.

Two implementations:
    RecordStoreSyncer         — synchronous, blocks hot path (~2-10ms per write)
    BufferedRecordStoreSyncer — async via WriteBuffer (~0.1ms amortized)

Architecture:
    Kernel → write_observer.on_write() → [OperationLogger + VersionRecorder]
    Kernel → write_observer.on_delete() → [OperationLogger + VersionRecorder]
    Error policy owned by kernel (audit_strict_mode). Syncer just raises on failure.

Issue #1246: BufferedRecordStoreSyncer implements Decision 13A (write-behind buffer).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.core._metadata_generated import FileMetadata

logger = logging.getLogger(__name__)


class RecordStoreSyncer:
    """Syncs Metastore writes to RecordStore (OperationLog + VersionHistory).

    Duck-typed write observer — kernel calls on_write/on_delete without
    knowing or importing this class.
    """

    def __init__(self, session_factory: Callable[..., Any]) -> None:
        self._session_factory = session_factory

    def on_write(
        self,
        metadata: FileMetadata,
        *,
        is_new: bool,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Sync a write operation to RecordStore.

        Raises on failure — caller (kernel) decides error policy.
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        with self._session_factory() as session:
            OperationLogger(session).log_operation(
                operation_type="write",
                path=path,
                zone_id=zone_id,
                agent_id=agent_id,
                snapshot_hash=snapshot_hash,
                metadata_snapshot=metadata_snapshot,
                status="success",
            )
            VersionRecorder(session).record_write(metadata, is_new=is_new)
            session.commit()

    def on_write_batch(
        self,
        items: list[tuple[FileMetadata, bool]],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Sync a batch write to RecordStore (single transaction).

        Args:
            items: List of (metadata, is_new) tuples.
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        with self._session_factory() as session:
            op_logger = OperationLogger(session)
            recorder = VersionRecorder(session)
            for metadata, is_new in items:
                op_logger.log_operation(
                    operation_type="write",
                    path=metadata.path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=metadata.etag,
                    metadata_snapshot=None,
                    status="success",
                )
                recorder.record_write(metadata, is_new=is_new)
            session.commit()

    def on_rename(
        self,
        old_path: str,
        new_path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Sync a rename operation to RecordStore.

        Raises on failure — caller (kernel) decides error policy.
        """
        from nexus.storage.operation_logger import OperationLogger

        with self._session_factory() as session:
            OperationLogger(session).log_operation(
                operation_type="rename",
                path=old_path,
                new_path=new_path,
                zone_id=zone_id,
                agent_id=agent_id,
                snapshot_hash=snapshot_hash,
                metadata_snapshot=metadata_snapshot,
                status="success",
            )
            session.commit()

    def on_delete(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Sync a delete operation to RecordStore.

        Raises on failure — caller (kernel) decides error policy.
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        with self._session_factory() as session:
            OperationLogger(session).log_operation(
                operation_type="delete",
                path=path,
                zone_id=zone_id,
                agent_id=agent_id,
                snapshot_hash=snapshot_hash,
                metadata_snapshot=metadata_snapshot,
                status="success",
            )
            VersionRecorder(session).record_delete(path)
            session.commit()


class BufferedRecordStoreSyncer:
    """Async write observer backed by WriteBuffer (Issue #1246, Decision 13A).

    Same duck-typed interface as RecordStoreSyncer, but enqueues events
    into a WriteBuffer instead of writing synchronously. Hot path returns
    immediately (~0.1ms amortized vs ~2-10ms synchronous).

    Must call start() before use and stop() on shutdown.
    """

    def __init__(
        self,
        session_factory: Callable[..., Any],
        *,
        flush_interval_ms: int = 100,
        max_buffer_size: int = 100,
        max_retries: int = 3,
    ) -> None:
        from nexus.storage.write_buffer import WriteBuffer

        self._buffer = WriteBuffer(
            session_factory,
            flush_interval_ms=flush_interval_ms,
            max_buffer_size=max_buffer_size,
            max_retries=max_retries,
        )

    def start(self) -> None:
        """Start the background flush thread."""
        self._buffer.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop and drain remaining events."""
        self._buffer.stop(timeout=timeout)

    @property
    def metrics(self) -> dict[str, int]:
        """Return buffer metrics."""
        return self._buffer.metrics

    def on_write(
        self,
        metadata: FileMetadata,
        *,
        is_new: bool,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a write event. Returns immediately."""
        self._buffer.enqueue_write(
            metadata,
            is_new=is_new,
            path=path,
            zone_id=zone_id,
            agent_id=agent_id,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=metadata_snapshot,
        )

    def on_write_batch(
        self,
        items: list[tuple[FileMetadata, bool]],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Enqueue a batch of write events. Returns immediately."""
        for metadata, is_new in items:
            self._buffer.enqueue_write(
                metadata,
                is_new=is_new,
                path=metadata.path,
                zone_id=zone_id,
                agent_id=agent_id,
                snapshot_hash=metadata.etag,
            )

    def on_rename(
        self,
        old_path: str,
        new_path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a rename event. Returns immediately."""
        self._buffer.enqueue_rename(
            old_path=old_path,
            new_path=new_path,
            zone_id=zone_id,
            agent_id=agent_id,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=metadata_snapshot,
        )

    def on_delete(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a delete event. Returns immediately."""
        self._buffer.enqueue_delete(
            path=path,
            zone_id=zone_id,
            agent_id=agent_id,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=metadata_snapshot,
        )
