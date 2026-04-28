"""RecordStore write observer — synchronous WriteObserverProtocol implementation.

Bundles OperationLogger + VersionRecorder into a single injectable observer.
Created by factory.py, injected into NexusFS kernel as write_observer.

The kernel calls on_write()/on_delete()/on_rename()/on_write_batch()/
on_mkdir()/on_rmdir() after Metastore mutations. This observer handles
all RecordStore side-effects in a single synchronous transaction.

The kernel passes kernel-native ``FileMetadata``.  The observer derives
``snapshot_hash`` (``metadata.content_id``) and ``metadata_snapshot``
(``metadata.to_dict()``) internally — these are RecordStore concerns.

Architecture:
    Kernel → write_observer.on_write() → [OperationLogger + VersionRecorder]
    Kernel → write_observer.on_delete() → [OperationLogger + VersionRecorder]
    Error policy owned by observer (strict_mode). Kernel is a pure caller.

MCL in operation_log (Issue #2929, Key Decision #2):
    Each operation_log row carries entity_urn / aspect_name / change_type.
    metadata_snapshot stores the NEW aspect value (for replay), not the old
    value. ``OperationLogger.replay_changes()`` is the single replay source.

For OBSERVE-phase batched implementation, see RecordStoreWriteObserver
in ``nexus.storage.piped_record_store_write_observer``.

Issue #900: Replaced snapshot_hash/metadata_snapshot params with metadata.
"""

import logging

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
        record_store: RecordStoreABC,
        *,
        strict_mode: bool = True,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._strict_mode = strict_mode

        # Post-flush hooks: called after successful commit (Issue #2978)
        # Same interface as the OBSERVE-phase RecordStoreWriteObserver so the factory
        # can wire extraction hooks regardless of which observer is active.
        self._post_flush_hooks: list = []

    def register_post_flush_hook(self, hook: object) -> None:
        """Register a callback invoked after each successful write commit.

        Hooks receive a list of event dicts (same shape as piped observer).
        They run AFTER the audit trail commit, so failures do not block
        the audit path. Used by CatalogService for on-write extraction.
        """
        self._post_flush_hooks.append(hook)

    def _run_post_flush_hooks(self, events: list) -> None:
        """Run post-flush hooks after a successful commit. Best-effort."""
        if not self._post_flush_hooks:
            return
        for hook in self._post_flush_hooks:
            try:
                hook(events)
            except Exception as hook_err:
                logger.debug(
                    "Post-flush hook %s failed (non-critical): %s",
                    getattr(hook, "__name__", hook),
                    hook_err,
                )

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

    async def flush(self, timeout: float = 5.0) -> int:  # noqa: ARG002
        """No-op — synchronous observer commits inline, nothing to flush."""
        return 0

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

        metadata_snapshot stores the NEW metadata (for MCL replay).
        snapshot_hash stores the old content_id (for CAS undo).
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
            with self._session_factory() as session:
                urn = self._build_urn(path, zone_id)
                OperationLogger(session).log_operation(
                    operation_type="write",
                    path=path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=old_metadata.content_id if old_metadata else None,
                    metadata_snapshot=metadata.to_dict(),
                    status="success",
                    entity_urn=urn,
                    aspect_name="file_metadata",
                    change_type="upsert",
                )
                VersionRecorder(session).record_write(metadata, is_new=is_new)
                session.commit()

            # Post-flush hooks: extraction, lineage, etc. (Issue #2978, #3417)
            self._run_post_flush_hooks(
                [
                    {
                        "op": "write",
                        "path": path,
                        "is_new": is_new,
                        "zone_id": zone_id,
                        "agent_id": agent_id,
                        "agent_generation": getattr(metadata, "agent_generation", None),
                        "metadata": metadata.to_dict(),
                    }
                ]
            )
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
                    urn = self._build_urn(metadata.path, zone_id)
                    op_logger.log_operation(
                        operation_type="write",
                        path=metadata.path,
                        zone_id=zone_id,
                        agent_id=agent_id,
                        snapshot_hash=metadata.content_id,
                        metadata_snapshot=metadata.to_dict(),
                        status="success",
                        flush=False,
                        entity_urn=urn,
                        aspect_name="file_metadata",
                        change_type="upsert",
                    )
                    recorder.record_write(metadata, is_new=is_new)

                session.commit()

            # Post-flush hooks: extraction, lineage, etc. (Issue #2978, #3417)
            self._run_post_flush_hooks(
                [
                    {
                        "op": "write",
                        "path": md.path,
                        "is_new": new,
                        "zone_id": zone_id,
                        "agent_id": agent_id,
                        "agent_generation": getattr(md, "agent_generation", None),
                        "metadata": md.to_dict(),
                    }
                    for md, new in items
                ]
            )
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

        URNs are locators (Issue #2929 Key Decision #3): rename changes the
        URN. Two operation_log rows: DELETE old URN + UPSERT new URN.
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
            with self._session_factory() as session:
                old_urn = self._build_urn(old_path, zone_id)
                new_urn = self._build_urn(new_path, zone_id)
                op_logger = OperationLogger(session)

                # Row 1: DELETE old locator
                op_logger.log_operation(
                    operation_type="rename",
                    path=old_path,
                    new_path=new_path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=metadata.content_id if metadata else None,
                    metadata_snapshot=metadata.to_dict() if metadata else None,
                    status="success",
                    entity_urn=old_urn,
                    aspect_name="file_metadata",
                    change_type="delete",
                )
                # Row 2: UPSERT new locator
                op_logger.log_operation(
                    operation_type="rename",
                    path=new_path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    metadata_snapshot=metadata.to_dict() if metadata else None,
                    status="success",
                    entity_urn=new_urn,
                    aspect_name="file_metadata",
                    change_type="upsert",
                )

                VersionRecorder(session).record_rename(old_path, new_path, zone_id=zone_id)

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

        Soft-deletes entity aspects (Issue #2929).
        """
        from nexus.storage.aspect_service import AspectService
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        try:
            with self._session_factory() as session:
                urn = self._build_urn(path, zone_id)
                OperationLogger(session).log_operation(
                    operation_type="delete",
                    path=path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=metadata.content_id if metadata else None,
                    metadata_snapshot=metadata.to_dict() if metadata else None,
                    status="success",
                    entity_urn=urn,
                    aspect_name="file_metadata",
                    change_type="delete",
                )
                VersionRecorder(session).record_delete(path)
                AspectService(session).soft_delete_entity_aspects(urn)
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

    @staticmethod
    def _build_urn(path: str, zone_id: str | None) -> str:
        """Build a locator URN for a file from its virtual path.

        Delegates to NexusURN.for_file() — single source of truth for
        URN construction (Issue #2978, Issue #2929 Key Decision #3).
        """
        from nexus.contracts.urn import NexusURN

        return str(NexusURN.for_file(zone_id or "default", path))

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
