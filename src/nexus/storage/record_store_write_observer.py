"""RecordStore write observer — synchronous WriteObserverProtocol implementation.

Bundles OperationLogger + VersionRecorder into a single injectable observer.
Created by factory.py, injected into NexusFS kernel as write_observer.

The kernel calls on_write()/on_delete()/on_rename()/on_write_batch()/
on_mkdir()/on_rmdir() after Metastore mutations. This observer handles
all RecordStore side-effects in a single synchronous transaction.

The kernel passes kernel-native ``FileMetadata``.  The observer derives
``snapshot_hash`` (``metadata.etag``) and ``metadata_snapshot``
(``metadata.to_dict()``) internally — these are RecordStore concerns.

Architecture:
    Kernel → write_observer.on_write() → [OperationLogger + VersionRecorder]
    Kernel → write_observer.on_delete() → [OperationLogger + VersionRecorder]
    Error policy owned by observer (strict_mode). Kernel is a pure caller.

For async DT_PIPE-backed implementation, see PipedRecordStoreWriteObserver
in ``nexus.storage.piped_record_store_write_observer``.

Issue #900: Replaced snapshot_hash/metadata_snapshot params with metadata.
"""

import logging
from typing import TYPE_CHECKING

from nexus.contracts.metadata import FileMetadata
from nexus.storage.record_store import RecordStoreABC

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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
        record_store: RecordStoreABC,
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
                "Error: %s. Set audit_strict_mode=False to allow writes without audit logs.",
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
        metadata: FileMetadata,
        *,
        is_new: bool,
        path: str,
        old_metadata: FileMetadata | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Sync a write operation to RecordStore.

        snapshot_hash/metadata_snapshot in the operation log store the PREVIOUS
        version (for undo).  Derived from old_metadata, not metadata.
        Also records MCL entry (fire-and-forget, non-critical).
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
            with self._session_factory() as session:
                OperationLogger(session).log_operation(
                    operation_type="write",
                    path=path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=old_metadata.etag if old_metadata else None,
                    metadata_snapshot=old_metadata.to_dict() if old_metadata else None,
                    status="success",
                )
                VersionRecorder(session).record_write(metadata, is_new=is_new)

                # MCL recording (Issue #2929) — non-critical, fire-and-forget
                self._record_mcl_write(
                    session,
                    metadata=metadata,
                    old_metadata=old_metadata,
                    zone_id=zone_id,
                    agent_id=agent_id,
                )

                session.commit()
        except Exception as e:
            self._handle_error("write", path, e)

    def on_write_batch(
        self,
        items: list[tuple[FileMetadata, bool]],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        urgency: str | None = None,  # noqa: ARG002
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
                        flush=False,
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
        metadata: FileMetadata | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Sync a rename operation to RecordStore.

        With UUID-based URN, rename doesn't change the URN — only the
        path aspect updates (Issue #2929).
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
            with self._session_factory() as session:
                OperationLogger(session).log_operation(
                    operation_type="rename",
                    path=old_path,
                    new_path=new_path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=metadata.etag if metadata else None,
                    metadata_snapshot=metadata.to_dict() if metadata else None,
                    status="success",
                )
                VersionRecorder(session).record_rename(old_path, new_path)

                # MCL recording (Issue #2929) — path aspect changed
                self._record_mcl_rename(
                    session,
                    old_path=old_path,
                    new_path=new_path,
                    metadata=metadata,
                    zone_id=zone_id,
                    agent_id=agent_id,
                )

                session.commit()
        except Exception as e:
            self._handle_error("rename", old_path, e)

    def on_delete(
        self,
        path: str,
        *,
        metadata: FileMetadata | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Sync a delete operation to RecordStore.

        Also records MCL entry and soft-deletes entity aspects (Issue #2929).
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
            with self._session_factory() as session:
                OperationLogger(session).log_operation(
                    operation_type="delete",
                    path=path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=metadata.etag if metadata else None,
                    metadata_snapshot=metadata.to_dict() if metadata else None,
                    status="success",
                )
                VersionRecorder(session).record_delete(path)

                # MCL recording (Issue #2929) — non-critical
                self._record_mcl_delete(
                    session,
                    path=path,
                    metadata=metadata,
                    zone_id=zone_id,
                    agent_id=agent_id,
                )

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

    # =========================================================================
    # MCL helpers (Issue #2929) — non-critical, failures logged not raised
    # =========================================================================

    def _record_mcl_write(
        self,
        session: "Session",  # noqa: F821
        *,
        metadata: FileMetadata,
        old_metadata: FileMetadata | None,
        zone_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Record MCL entry for a file write. Non-critical."""
        try:
            from nexus.storage.mcl_recorder import MCLRecorder

            # Construct URN from path (service layer will use path_id in production)
            urn = f"urn:nexus:file:{zone_id or 'default'}:{metadata.path}"
            MCLRecorder(session).record_file_write(
                entity_urn=urn,
                metadata_dict=metadata.to_dict(),
                zone_id=zone_id,
                changed_by=agent_id or "system",
                previous_metadata=old_metadata.to_dict() if old_metadata else None,
            )
        except Exception:
            logger.debug("MCL write recording failed (non-critical)", exc_info=True)

    def _record_mcl_delete(
        self,
        session: "Session",  # noqa: F821
        *,
        path: str,
        metadata: FileMetadata | None,
        zone_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Record MCL entry for a file delete. Non-critical."""
        try:
            from nexus.storage.mcl_recorder import MCLRecorder

            urn = f"urn:nexus:file:{zone_id or 'default'}:{path}"
            MCLRecorder(session).record_file_delete(
                entity_urn=urn,
                zone_id=zone_id,
                changed_by=agent_id or "system",
                previous_metadata=metadata.to_dict() if metadata else None,
            )
        except Exception:
            logger.debug("MCL delete recording failed (non-critical)", exc_info=True)

    def _record_mcl_rename(
        self,
        session: "Session",  # noqa: F821
        *,
        old_path: str,
        new_path: str,
        metadata: FileMetadata | None,  # noqa: ARG002
        zone_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Record MCL entry for a file rename (path aspect change). Non-critical."""
        try:
            from nexus.storage.mcl_recorder import MCLRecorder

            urn = f"urn:nexus:file:{zone_id or 'default'}:{old_path}"
            MCLRecorder(session).record_file_rename(
                entity_urn=urn,
                old_path=old_path,
                new_path=new_path,
                zone_id=zone_id,
                changed_by=agent_id or "system",
            )
        except Exception:
            logger.debug("MCL rename recording failed (non-critical)", exc_info=True)

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
