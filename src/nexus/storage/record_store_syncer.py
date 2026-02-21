"""RecordStore write observers — WriteObserverProtocol implementations.

Bundles OperationLogger + VersionRecorder into a single injectable observer.
Created by factory.py, injected into NexusFS kernel as write_observer.

The kernel calls on_write()/on_delete()/on_rename()/on_write_batch()/
on_mkdir()/on_rmdir() after Metastore mutations. These observers handle
all RecordStore side-effects in a single transaction.

Two implementations:
    RecordStoreWriteObserver         — synchronous, blocks hot path (~2-10ms)
    BufferedRecordStoreWriteObserver — async via WriteBuffer (~0.1ms amortized)

Architecture:
    Kernel → write_observer.on_write() → [OperationLogger + VersionRecorder]
    Kernel → write_observer.on_delete() → [OperationLogger + VersionRecorder]
    Error policy owned by observer (strict_mode). Kernel is a pure caller.

Issue #1246: BufferedRecordStoreWriteObserver implements Decision 13A (write-behind buffer).
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class RecordStoreWriteObserver:
    """Syncs Metastore writes to RecordStore (OperationLog + VersionHistory).

    Implements WriteObserverProtocol. Kernel calls on_write/on_delete
    without knowing or importing this class.

    Error policy:
        strict_mode=True  → raise AuditLogError on failure (P0 compliance)
        strict_mode=False → log CRITICAL warning, continue (high-availability)
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        *,
        strict_mode: bool = True,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._strict_mode = strict_mode

    def _handle_error(self, operation: str, path: str, error: Exception) -> None:
        """Apply audit error policy: raise or log depending on strict_mode."""
        from nexus.contracts.exceptions import AuditLogError

        if self._strict_mode:
            logger.error(
                "AUDIT LOG FAILURE: %s on '%s' ABORTED. "
                "Error: %s. Set AuditConfig(strict_mode=False) to allow writes without audit logs.",
                operation,
                path,
                error,
            )
            raise AuditLogError(
                f"Operation aborted: audit logging failed for {operation}: {error}",
                path=path,
                original_error=error,
            ) from error
        else:
            logger.critical(
                "AUDIT LOG FAILURE: %s on '%s' SUCCEEDED but audit log FAILED. "
                "Error: %s. This creates an audit trail gap!",
                operation,
                path,
                error,
            )

    def on_write(
        self,
        metadata: "FileMetadata",
        *,
        is_new: bool,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
        urgency: str | None = None,  # noqa: ARG002 — Protocol conformance; sync path ignores urgency
    ) -> None:
        """Sync a write operation to RecordStore."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
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
        except Exception as e:
            self._handle_error("write", path, e)

    def on_write_batch(
        self,
        items: "list[tuple[FileMetadata, bool]]",
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        urgency: str | None = None,  # noqa: ARG002 — Protocol conformance; sync path ignores urgency
    ) -> None:
        """Sync a batch write to RecordStore (single transaction).

        Args:
            items: List of (metadata, is_new) tuples.
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
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
                        flush=False,  # Defer flush — commit handles it
                    )
                    recorder.record_write(metadata, is_new=is_new)
                session.commit()
        except Exception as e:
            first_path = items[0][0].path if items else "<batch>"
            self._handle_error("write_batch", first_path, e)

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
        """Sync a rename operation to RecordStore."""
        from nexus.storage.operation_logger import OperationLogger

        try:
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
        except Exception as e:
            self._handle_error("rename", old_path, e)

    def on_delete(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Sync a delete operation to RecordStore."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
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
        except Exception as e:
            self._handle_error("delete", path, e)

    def on_mkdir(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Sync a mkdir operation to RecordStore (audit log only, no versioning)."""
        from nexus.storage.operation_logger import OperationLogger

        try:
            with self._session_factory() as session:
                OperationLogger(session).log_operation(
                    operation_type="mkdir",
                    path=path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    status="success",
                )
                session.commit()
        except Exception as e:
            self._handle_error("mkdir", path, e)

    def on_rmdir(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        recursive: bool = False,
    ) -> None:
        """Sync a rmdir operation to RecordStore (audit log only, no versioning)."""
        from nexus.storage.operation_logger import OperationLogger

        op_type = "rmdir_recursive" if recursive else "rmdir"
        try:
            with self._session_factory() as session:
                OperationLogger(session).log_operation(
                    operation_type=op_type,
                    path=path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    status="success",
                )
                session.commit()
        except Exception as e:
            self._handle_error("rmdir", path, e)


class BufferedRecordStoreWriteObserver:
    """Async write observer backed by WriteBuffer (Issue #1246, Decision 13A).

    Same WriteObserverProtocol interface as RecordStoreWriteObserver, but
    enqueues events into a WriteBuffer instead of writing synchronously.
    Hot path returns immediately (~0.1ms amortized vs ~2-10ms synchronous).

    Error policy: strict_mode is stored for API consistency. Since enqueue
    never fails, the error policy applies only to background flush failures
    handled by WriteBuffer (retry + drop with logging).

    Must call start() before use and stop() on shutdown.
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        *,
        strict_mode: bool = True,
        flush_interval_ms: int = 100,
        max_buffer_size: int = 100,
        max_retries: int = 3,
    ) -> None:
        from nexus.storage.write_buffer import WriteBuffer

        self._strict_mode = strict_mode
        self._buffer = WriteBuffer(
            record_store,
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
    def metrics(self) -> dict[str, int | float | dict[str, int]]:
        """Return buffer metrics."""
        return self._buffer.metrics

    def on_write(
        self,
        metadata: "FileMetadata",
        *,
        is_new: bool,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
        urgency: str | None = None,
    ) -> None:
        """Enqueue a write event. Returns immediately."""
        from nexus.storage.write_buffer import Urgency

        _urgency = Urgency.HIGH if urgency == "high" else Urgency.NORMAL
        self._buffer.enqueue_write(
            metadata,
            is_new=is_new,
            path=path,
            zone_id=zone_id,
            agent_id=agent_id,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=metadata_snapshot,
            urgency=_urgency,
        )

    def on_write_batch(
        self,
        items: "list[tuple[FileMetadata, bool]]",
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        urgency: str | None = None,
    ) -> None:
        """Enqueue a batch of write events. Returns immediately."""
        from nexus.storage.write_buffer import Urgency

        _urgency = Urgency.HIGH if urgency == "high" else Urgency.NORMAL
        for metadata, is_new in items:
            self._buffer.enqueue_write(
                metadata,
                is_new=is_new,
                path=metadata.path,
                zone_id=zone_id,
                agent_id=agent_id,
                snapshot_hash=metadata.etag,
                urgency=_urgency,
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

    def on_mkdir(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Enqueue a mkdir event. Returns immediately."""
        self._buffer.enqueue_mkdir(path=path, zone_id=zone_id, agent_id=agent_id)

    def on_rmdir(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        recursive: bool = False,
    ) -> None:
        """Enqueue a rmdir event. Returns immediately."""
        self._buffer.enqueue_rmdir(
            path=path, zone_id=zone_id, agent_id=agent_id, recursive=recursive
        )
