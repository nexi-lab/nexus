"""OBSERVE-phase observer for RecordStore audit trail + versioning.

Receives FILE_WRITE / FILE_DELETE / FILE_RENAME / DIR_CREATE / DIR_DELETE
events from the Rust kernel and flushes them to RecordStore in debounced
batches.

Issue #809: Decouple write_observer.on_write() sync DB write from hot path.

Architecture:
    Rust kernel sys_write / sys_unlink / sys_mkdir / sys_rmdir
      -> dispatch_observers (Rust MutationObserver trait)
        -> accumulate event in deque + reset debounce timer
        -> threading.Timer fires _flush()
        -> single RecordStore transaction (OperationLogger + VersionRecorder)
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.file_events import FileEvent
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# Consumer batch processing
_MAX_BATCH_DRAIN = 100  # Max events to drain per flush
_MAX_RETRIES = 3
_DEBOUNCE_S = 0.2  # Debounce window (200ms) — same as former linger_s


def _metadata_from_dict(d: dict[str, Any]) -> Any:
    """Reconstruct FileMetadata from to_dict() output."""
    from nexus.contracts.metadata import FileMetadata

    if d.get("created_at") and isinstance(d["created_at"], str):
        d["created_at"] = datetime.fromisoformat(d["created_at"])
    if d.get("modified_at") and isinstance(d["modified_at"], str):
        d["modified_at"] = datetime.fromisoformat(d["modified_at"])
    return FileMetadata(**d)


class RecordStoreWriteObserver:
    """OBSERVE-phase observer for RecordStore audit trail + versioning.

    Receives mutation events from the Rust kernel and flushes them to
    RecordStore (OperationLogger + VersionRecorder) in debounced batches.

    Registration:
        Enlisted via factory orchestrator; events dispatched by the Rust
        kernel's MutationObserver trait (not Python on_mutation).
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        *,
        strict_mode: bool = True,
        event_signal: "Any | None" = None,
        debounce_seconds: float = _DEBOUNCE_S,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._strict_mode = strict_mode
        self._event_signal = event_signal  # Issue #3193: wake delivery worker
        self._debounce = debounce_seconds

        # Debounce state — protected by _lock
        self._pending: deque[dict[str, Any]] = deque(maxlen=10_000)
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

        # Metrics
        self._total_flushed = 0
        self._total_failed = 0
        self._total_retries = 0
        self._total_dropped = 0

        # Post-flush hooks: called after successful commit (Issue #2978)
        # Used by CatalogService for async-on-write extraction
        self._post_flush_hooks: list[Any] = []

    def register_post_flush_hook(self, hook: Any) -> None:
        """Register a callback invoked after each successful flush.

        Hooks receive the list of flushed events. They run AFTER the
        audit trail commit, so failures do not block the audit path.
        Used by CatalogService for async-on-write extraction (Issue #2978).
        """
        self._post_flush_hooks.append(hook)

    # ------------------------------------------------------------------
    # Event intake — called by SyncAuditWriteInterceptor post-hooks
    # ------------------------------------------------------------------

    def _enqueue(self, event: dict[str, Any]) -> None:
        """Add event to pending deque and reset debounce timer."""
        with self._lock:
            self._pending.append(event)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def on_write(
        self,
        metadata: Any,
        *,
        is_new: bool,
        path: str,
        old_metadata: Any | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Accept a write event from SyncAuditWriteInterceptor."""
        self._enqueue(
            {
                "op": "write",
                "path": path,
                "is_new": is_new,
                "zone_id": zone_id,
                "agent_id": agent_id,
                "snapshot_hash": old_metadata.etag if old_metadata else None,
                "metadata_snapshot": old_metadata.to_dict() if old_metadata else None,
                "metadata": metadata.to_dict() if hasattr(metadata, "to_dict") else metadata,
            }
        )

    def on_write_batch(
        self,
        items: list[tuple[Any, bool]],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        urgency: str | None = None,  # noqa: ARG002
    ) -> None:
        """Accept a batch write event from SyncAuditWriteInterceptor."""
        for metadata, is_new in items:
            self._enqueue(
                {
                    "op": "write",
                    "path": metadata.path,
                    "is_new": is_new,
                    "zone_id": zone_id,
                    "agent_id": agent_id,
                    "snapshot_hash": None,
                    "metadata_snapshot": None,
                    "metadata": metadata.to_dict() if hasattr(metadata, "to_dict") else metadata,
                }
            )

    def on_delete(
        self,
        *,
        path: str,
        metadata: Any | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Accept a delete event from SyncAuditWriteInterceptor."""
        self._enqueue(
            {
                "op": "delete",
                "path": path,
                "zone_id": zone_id,
                "agent_id": agent_id,
                "snapshot_hash": metadata.etag if metadata else None,
                "metadata_snapshot": metadata.to_dict()
                if metadata and hasattr(metadata, "to_dict")
                else None,
            }
        )

    def on_rename(
        self,
        *,
        old_path: str,
        new_path: str,
        metadata: Any | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Accept a rename event from SyncAuditWriteInterceptor."""
        self._enqueue(
            {
                "op": "rename",
                "path": old_path,
                "new_path": new_path,
                "zone_id": zone_id,
                "agent_id": agent_id,
                "snapshot_hash": metadata.etag if metadata else None,
                "metadata_snapshot": metadata.to_dict()
                if metadata and hasattr(metadata, "to_dict")
                else None,
            }
        )

    def on_mkdir(
        self,
        *,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Accept a mkdir event from SyncAuditWriteInterceptor."""
        self._enqueue(
            {
                "op": "mkdir",
                "path": path,
                "zone_id": zone_id,
                "agent_id": agent_id,
            }
        )

    def on_rmdir(
        self,
        *,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
        recursive: bool = False,
    ) -> None:
        """Accept an rmdir event from SyncAuditWriteInterceptor."""
        self._enqueue(
            {
                "op": "rmdir",
                "path": path,
                "zone_id": zone_id,
                "agent_id": agent_id,
                "recursive": recursive,
            }
        )

    # ------------------------------------------------------------------
    # Debounce flush
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Fire after debounce window -- flush events to RecordStore."""
        with self._lock:
            events = list(self._pending)
            self._pending.clear()
            self._timer = None

        if not events:
            return

        # Batch: take up to _MAX_BATCH_DRAIN at a time, retry remainder
        self._flush_batch(events)

    def _flush_batch(self, events: list[dict[str, Any]], attempt: int = 0) -> None:
        """Flush a batch of events to RecordStore in a single transaction."""
        t0 = time.monotonic()
        try:
            self._flush_batch_sync(events)

            duration = time.monotonic() - t0
            self._total_flushed += len(events)

            # Issue #3193: signal delivery worker immediately after commit
            if self._event_signal is not None:
                self._event_signal.set()

            logger.info(
                "[OBSERVE] Flushed %d audit events in %.3fs",
                len(events),
                duration,
            )

            # Post-flush hooks: extraction, etc. (Issue #2978)
            # Runs AFTER commit — failures do not block audit trail.
            for hook in self._post_flush_hooks:
                try:
                    hook(events)
                except Exception as hook_err:
                    logger.debug(
                        "Post-flush hook %s failed (non-critical): %s",
                        getattr(hook, "__name__", hook),
                        hook_err,
                    )

        except Exception as e:
            if attempt < _MAX_RETRIES:
                self._total_retries += 1
                wait = 0.1 * (2**attempt)  # 100ms, 200ms, 400ms
                logger.warning(
                    "RecordStoreWriteObserver flush failed (attempt %d/%d, retry in %.1fs): %s",
                    attempt + 1,
                    _MAX_RETRIES,
                    wait,
                    e,
                )
                threading.Timer(wait, self._flush_batch, args=(events, attempt + 1)).start()
            else:
                self._total_failed += len(events)
                logger.error(
                    "RecordStoreWriteObserver flush FAILED after %d retries, "
                    "dropping %d events: %s",
                    _MAX_RETRIES,
                    len(events),
                    e,
                )

    # ------------------------------------------------------------------
    # Flush sync (for CLI shutdown / close callbacks)
    # ------------------------------------------------------------------

    def flush_sync(self) -> int:
        """Synchronously flush all pending events to the DB.

        Used by CLI shutdown path (NexusFS.close) where no asyncio loop
        is running. Returns count of flushed events.
        """
        with self._lock:
            events = list(self._pending)
            self._pending.clear()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        if not events:
            return 0

        try:
            self._flush_batch_sync(events)
            count = len(events)
            self._total_flushed += count
            logger.debug("flush_sync: flushed %d events", count)
            return count
        except Exception as e:
            logger.error("flush_sync failed, %d events lost: %s", len(events), e)
            self._total_failed += len(events)
            return 0

    async def flush(self, timeout: float = 5.0) -> int:  # noqa: ARG002
        """Flush pending events. Async signature for protocol compat.

        Delegates to flush_sync() since no pipe draining is needed.
        """
        return self.flush_sync()

    # ------------------------------------------------------------------
    # Event conversion: FileEvent -> audit dict
    # ------------------------------------------------------------------

    @staticmethod
    def _file_event_to_dict(event: "FileEvent") -> dict[str, Any] | None:
        """Convert a kernel FileEvent to the dict format used by _process_events_in_session."""
        from nexus.core.file_events import FileEventType

        etype = event.type if isinstance(event.type, str) else event.type.value

        if etype == FileEventType.FILE_WRITE:
            return {
                "op": "write",
                "path": event.path,
                "is_new": event.is_new,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "snapshot_hash": event.old_etag,
                "metadata_snapshot": None,
                "metadata": event.to_dict(),
            }
        elif etype == FileEventType.FILE_DELETE:
            return {
                "op": "delete",
                "path": event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "snapshot_hash": event.etag,
                "metadata_snapshot": None,
            }
        elif etype == FileEventType.FILE_RENAME:
            return {
                "op": "rename",
                "path": event.old_path or event.path,
                "new_path": event.new_path or event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "snapshot_hash": event.etag,
                "metadata_snapshot": None,
            }
        elif etype == FileEventType.DIR_CREATE:
            return {
                "op": "mkdir",
                "path": event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
            }
        elif etype == FileEventType.DIR_DELETE:
            return {
                "op": "rmdir",
                "path": event.path,
                "zone_id": event.zone_id,
                "agent_id": event.agent_id,
                "recursive": False,
            }
        else:
            return None  # Unsupported event type — ignore

    # ------------------------------------------------------------------
    # DB flush logic (shared by _flush_batch and flush_sync)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_urn(path: str, zone_id: str | None) -> str:
        """Build a locator URN for a file from its virtual path.

        Delegates to NexusURN.for_file() -- single source of truth for
        URN construction (Issue #2978, Issue #2929 Key Decision #3).
        """
        from nexus.contracts.urn import NexusURN

        return str(NexusURN.for_file(zone_id or "default", path))

    def _record_mcl_for_event(
        self,
        session: Any,
        event: dict[str, Any],
    ) -> None:
        """Record MCL entry for a single event. Non-critical, uses savepoint.

        MCL failures must NEVER corrupt the outer session transaction.
        """
        try:
            from nexus.storage.mcl_recorder import MCLRecorder

            op = event["op"]
            zone_id = event.get("zone_id")
            agent_id = event.get("agent_id")
            path = event["path"]
            changed_by = agent_id or "system"

            with session.begin_nested():
                recorder = MCLRecorder(session)
                if op == "write":
                    urn = self._build_urn(path, zone_id)
                    recorder.record_file_write(
                        entity_urn=urn,
                        metadata_dict=event.get("metadata"),
                        zone_id=zone_id,
                        changed_by=changed_by,
                        previous_metadata=event.get("metadata_snapshot"),
                    )
                elif op == "delete":
                    from nexus.storage.aspect_service import AspectService

                    urn = self._build_urn(path, zone_id)
                    recorder.record_file_delete(
                        entity_urn=urn,
                        zone_id=zone_id,
                        changed_by=changed_by,
                        previous_metadata=event.get("metadata_snapshot"),
                    )
                    AspectService(session).soft_delete_entity_aspects(urn)
                elif op == "rename":
                    old_urn = self._build_urn(path, zone_id)
                    new_path = event.get("new_path", "")
                    new_urn = self._build_urn(new_path, zone_id)
                    recorder.record_file_delete(
                        entity_urn=old_urn,
                        zone_id=zone_id,
                        changed_by=changed_by,
                        previous_metadata=event.get("metadata_snapshot"),
                    )
                    recorder.record_file_write(
                        entity_urn=new_urn,
                        metadata_dict=event.get("metadata_snapshot"),
                        zone_id=zone_id,
                        changed_by=changed_by,
                    )
        except Exception:
            logger.debug(
                "MCL recording failed for %s:%s (non-critical)", event.get("op"), event.get("path")
            )

    def _process_events_in_session(self, session: Any, events: list[dict[str, Any]]) -> None:
        """Dispatch events to OperationLogger + VersionRecorder within a session.

        Shared by ``_flush_batch()`` and ``flush_sync()``.
        Caller is responsible for ``session.commit()``.
        """
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        op_logger = OperationLogger(session)
        recorder = VersionRecorder(session)

        for event in events:
            op = event["op"]
            zone_id = event.get("zone_id")
            agent_id = event.get("agent_id")

            if op == "write":
                urn = self._build_urn(event["path"], zone_id)
                op_logger.log_operation(
                    operation_type="write",
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=event.get("snapshot_hash"),
                    metadata_snapshot=event.get("metadata"),
                    status="success",
                    entity_urn=urn,
                    aspect_name="file_metadata",
                    change_type="upsert",
                )
                md = _metadata_from_dict(event["metadata"])
                recorder.record_write(md, is_new=event["is_new"])

            elif op == "delete":
                urn = self._build_urn(event["path"], zone_id)
                op_logger.log_operation(
                    operation_type="delete",
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=event.get("snapshot_hash"),
                    metadata_snapshot=event.get("metadata_snapshot"),
                    status="success",
                    entity_urn=urn,
                    aspect_name="file_metadata",
                    change_type="delete",
                )
                recorder.record_delete(event["path"])
                from nexus.storage.aspect_service import AspectService

                AspectService(session).soft_delete_entity_aspects(urn)

            elif op == "rename":
                old_urn = self._build_urn(event["path"], zone_id)
                new_path = event.get("new_path", "")
                new_urn = self._build_urn(new_path, zone_id)
                op_logger.log_operation(
                    operation_type="rename",
                    path=event["path"],
                    new_path=new_path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    snapshot_hash=event.get("snapshot_hash"),
                    metadata_snapshot=event.get("metadata_snapshot"),
                    status="success",
                    entity_urn=old_urn,
                    aspect_name="file_metadata",
                    change_type="delete",
                )
                op_logger.log_operation(
                    operation_type="rename",
                    path=new_path,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    metadata_snapshot=event.get("metadata_snapshot"),
                    status="success",
                    entity_urn=new_urn,
                    aspect_name="file_metadata",
                    change_type="upsert",
                )
                if new_path:
                    recorder.record_rename(event["path"], new_path, zone_id=zone_id)

            elif op == "mkdir":
                op_logger.log_operation(
                    operation_type="mkdir",
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    status="success",
                )

            elif op == "rmdir":
                op_type = "rmdir_recursive" if event.get("recursive") else "rmdir"
                op_logger.log_operation(
                    operation_type=op_type,
                    path=event["path"],
                    zone_id=zone_id,
                    agent_id=agent_id,
                    status="success",
                )

    def _flush_batch_sync(self, events: list[dict[str, Any]]) -> None:
        """Synchronous flush: critical writes first, MCL second.

        Phase 1 commits operation_log + file_paths + version_history.
        Phase 2 records MCL entries in a separate session so failures
        cannot corrupt the critical writes.
        """
        # Phase 1: Critical writes (operation_log + version_history)
        session = self._session_factory()
        try:
            self._process_events_in_session(session, events)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # Phase 2: MCL recording (non-critical, separate session)
        mcl_session = None
        try:
            mcl_session = self._session_factory()
            for event in events:
                if event.get("op") in ("write", "delete", "rename"):
                    self._record_mcl_for_event(mcl_session, event)
            mcl_session.commit()
        except Exception as mcl_err:
            if mcl_session is not None:
                mcl_session.rollback()
            logger.debug("MCL batch recording failed (non-critical): %s", mcl_err)
        finally:
            if mcl_session is not None:
                mcl_session.close()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Cancel any pending debounce timer (for clean shutdown)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> dict[str, int]:
        """Return observer metrics."""
        return {
            "total_flushed": self._total_flushed,
            "total_failed": self._total_failed,
            "total_retries": self._total_retries,
            "total_dropped": self._total_dropped,
            "pending_events": len(self._pending),
        }


# Backward-compat alias so existing imports don't break during migration
PipedRecordStoreWriteObserver = RecordStoreWriteObserver
