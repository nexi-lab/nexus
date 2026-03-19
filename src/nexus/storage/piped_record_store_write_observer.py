"""DT_PIPE-backed write observer — async RecordStore sync via kernel IPC.

Replaces BufferedRecordStoreWriteObserver's WriteBuffer (deque + threading.Thread)
with DT_PIPE kernel IPC (~5us per enqueue vs ~0.1ms amortized).

Implements WriteObserverProtocol. The kernel calls on_write()/on_delete()/
on_rename()/on_write_batch()/on_mkdir()/on_rmdir() synchronously after
Metastore mutations. This observer serializes each event to JSON and writes
it into a DT_PIPE ring buffer via PipeManager.pipe_write_nowait() (~5us).

A background asyncio consumer task drains the pipe, batches events, and
flushes them to RecordStore in a single transaction:
  - OperationLogger: audit trail (who did what, when)
  - VersionRecorder: file version history

Issue #809: Decouple write_observer.on_write() sync DB write from hot path.
Issue #808: Follows WorkflowDispatchService DT_PIPE pattern.

Architecture:
    Kernel hot path (sync)
      -> PipedRecordStoreWriteObserver.on_write()
        -> JSON serialize -> pipe_write_nowait()  # ~5us
        -> PipeFullError? -> drop + warn

    Background consumer (async)
      -> _consume() loop
        -> pipe_read() (async, blocking)
        -> batch coalesce (drain available events)
        -> single RecordStore transaction (OperationLogger + VersionRecorder)
        -> retry with exponential backoff on failure
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.metadata import FileMetadata

if TYPE_CHECKING:
    from nexus.core.pipe_manager import PipeManager
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# Pipe path and capacity (parallel to WorkflowDispatchService constants)
_AUDIT_PIPE_PATH = "/nexus/pipes/audit-events"
_AUDIT_PIPE_CAPACITY = 65_536  # 64KB

# Consumer batch processing
_MAX_BATCH_DRAIN = 100  # Max events to drain per batch
_MAX_RETRIES = 3


def _metadata_from_dict(d: dict[str, Any]) -> FileMetadata:
    """Reconstruct FileMetadata from to_dict() output."""
    if d.get("created_at") and isinstance(d["created_at"], str):
        d["created_at"] = datetime.fromisoformat(d["created_at"])
    if d.get("modified_at") and isinstance(d["modified_at"], str):
        d["modified_at"] = datetime.fromisoformat(d["modified_at"])
    return FileMetadata(**d)


class PipedRecordStoreWriteObserver:
    """DT_PIPE-backed write observer for async RecordStore sync.

    Implements WriteObserverProtocol. Enqueues events into a DT_PIPE ring
    buffer via PipeManager (~5us). A background consumer flushes batches
    to RecordStore (OperationLogger + VersionRecorder).

    Lifecycle:
        1. Created in factory (system tier) with record_store
        2. PipeManager injected via set_pipe_manager() (deferred)
        3. start() creates pipe, drains pre-startup buffer, spawns consumer
        4. stop() cancels consumer, flushes remaining events
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        *,
        strict_mode: bool = True,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._strict_mode = strict_mode

        # Pipe state (deferred injection)
        self._pipe_manager: PipeManager | None = None
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None

        # Pre-startup buffer: holds events before pipe is ready
        self._pre_buffer: deque[bytes] = deque(maxlen=1000)

        # Metrics
        self._total_enqueued = 0
        self._total_flushed = 0
        self._total_failed = 0
        self._total_retries = 0
        self._total_dropped = 0

        # Post-flush hooks: called after successful commit (Issue #2978)
        # Used by CatalogService for async-on-write extraction
        self._post_flush_hooks: list[Any] = []

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def set_pipe_manager(self, pm: "PipeManager") -> None:
        """Inject PipeManager after factory boot."""
        self._pipe_manager = pm

    def register_post_flush_hook(self, hook: Any) -> None:
        """Register a callback invoked after each successful flush.

        Hooks receive the list of flushed events. They run AFTER the
        audit trail commit, so failures do not block the audit path.
        Used by CatalogService for async-on-write extraction (Issue #2978).
        """
        self._post_flush_hooks.append(hook)

    # ------------------------------------------------------------------
    # WriteObserverProtocol — sync hot path
    # ------------------------------------------------------------------

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
        """Enqueue a write event via DT_PIPE. Returns in ~5us."""
        event = {
            "op": "write",
            "path": path,
            "is_new": is_new,
            "zone_id": zone_id,
            "agent_id": agent_id,
            "snapshot_hash": old_metadata.etag if old_metadata else None,
            "metadata_snapshot": old_metadata.to_dict() if old_metadata else None,
            "metadata": metadata.to_dict(),
        }
        self._enqueue(event)

    def on_write_batch(
        self,
        items: list[tuple[FileMetadata, bool]],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        urgency: str | None = None,  # noqa: ARG002
    ) -> None:
        """Enqueue a batch of write events via DT_PIPE."""
        for metadata, is_new in items:
            event = {
                "op": "write",
                "path": metadata.path,
                "is_new": is_new,
                "zone_id": zone_id,
                "agent_id": agent_id,
                "snapshot_hash": metadata.etag,
                "metadata": metadata.to_dict(),
            }
            self._enqueue(event)

    def on_rename(
        self,
        old_path: str,
        new_path: str,
        *,
        metadata: FileMetadata | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Enqueue a rename event via DT_PIPE."""
        event = {
            "op": "rename",
            "path": old_path,
            "new_path": new_path,
            "zone_id": zone_id,
            "agent_id": agent_id,
            "snapshot_hash": metadata.etag if metadata else None,
            "metadata_snapshot": metadata.to_dict() if metadata else None,
        }
        self._enqueue(event)

    def on_delete(
        self,
        path: str,
        *,
        metadata: FileMetadata | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Enqueue a delete event via DT_PIPE."""
        event = {
            "op": "delete",
            "path": path,
            "zone_id": zone_id,
            "agent_id": agent_id,
            "snapshot_hash": metadata.etag if metadata else None,
            "metadata_snapshot": metadata.to_dict() if metadata else None,
        }
        self._enqueue(event)

    def on_mkdir(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Enqueue a mkdir event via DT_PIPE."""
        self._enqueue({"op": "mkdir", "path": path, "zone_id": zone_id, "agent_id": agent_id})

    def on_rmdir(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        recursive: bool = False,
    ) -> None:
        """Enqueue a rmdir event via DT_PIPE."""
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
    # Internal enqueue
    # ------------------------------------------------------------------

    def _enqueue(self, event: dict[str, Any]) -> None:
        """Serialize and write to pipe (or pre-startup buffer)."""
        data = json.dumps(event).encode()
        self._total_enqueued += 1

        if self._pipe_manager is not None and self._pipe_ready:
            from nexus.core.pipe import PipeClosedError, PipeFullError

            try:
                self._pipe_manager.pipe_write_nowait(_AUDIT_PIPE_PATH, data)
            except PipeClosedError:
                # Pipe closing — buffer for flush_sync() to pick up
                self._pre_buffer.append(data)
            except PipeFullError:
                self._total_dropped += 1
                logger.warning(
                    "Audit pipe full, dropping event: %s:%s", event.get("op"), event.get("path")
                )
        else:
            # Pre-startup: buffer in memory (deque with maxlen=1000)
            self._pre_buffer.append(data)

    # ------------------------------------------------------------------
    # Async lifecycle (follows WorkflowDispatchService pattern)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create audit pipe, drain pre-startup buffer, spawn consumer."""
        if self._pipe_ready:
            return

        if self._pipe_manager is None:
            return  # CLI mode — no pipe manager

        from nexus.core.pipe import PipeError

        try:
            self._pipe_manager.create(
                _AUDIT_PIPE_PATH,
                capacity=_AUDIT_PIPE_CAPACITY,
                owner_id="kernel",
            )
        except PipeError:
            self._pipe_manager.open(_AUDIT_PIPE_PATH, capacity=_AUDIT_PIPE_CAPACITY)

        self._pipe_ready = True

        # Drain pre-startup buffer into pipe
        from nexus.core.pipe import PipeFullError

        while self._pre_buffer:
            data = self._pre_buffer.popleft()
            try:
                self._pipe_manager.pipe_write_nowait(_AUDIT_PIPE_PATH, data)
            except PipeFullError:
                self._total_dropped += 1
                logger.warning("Audit pipe full during pre-startup drain, dropping event")

        self._consumer_task = asyncio.create_task(self._consume())

    async def flush(self, timeout: float = 5.0) -> int:
        """Drain the pipe and flush all pending events to RecordStore.

        Blocks until all enqueued events have been committed to the database,
        ensuring that subsequent queries (e.g. list_versions) see the data.

        This fixes the race condition where sys_write() returns before the
        background consumer has flushed version records to the DB.

        Args:
            timeout: Maximum seconds to wait for events to appear. Defaults to 5.

        Returns:
            Number of events flushed.
        """
        if not self._pipe_ready or self._pipe_manager is None:
            # Pipe not started — flush pre-buffer directly via sync path
            return self._flush_pre_buffer_sync()

        from nexus.core.pipe import PipeClosedError, PipeEmptyError, PipeNotFoundError

        # Drain all available events from the pipe (non-blocking)
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                data = await self._pipe_manager.pipe_read(_AUDIT_PIPE_PATH, blocking=False)
                batch.append(json.loads(data))
            except (PipeEmptyError, PipeClosedError, PipeNotFoundError):
                break

        if batch:
            await self._flush_batch(batch)

        return len(batch)

    def _flush_pre_buffer_sync(self) -> int:
        """Flush pre-startup buffer directly to RecordStore (synchronous)."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        if not self._pre_buffer:
            return 0

        events = [json.loads(data) for data in self._pre_buffer]
        self._pre_buffer.clear()

        try:
            with self._session_factory() as session:
                op_logger = OperationLogger(session)
                recorder = VersionRecorder(session)

                for event in events:
                    op = event["op"]
                    zone_id = event.get("zone_id")
                    agent_id = event.get("agent_id")

                    if op == "write":
                        op_logger.log_operation(
                            operation_type="write",
                            path=event["path"],
                            zone_id=zone_id,
                            agent_id=agent_id,
                            snapshot_hash=event.get("snapshot_hash"),
                            metadata_snapshot=event.get("metadata_snapshot"),
                            status="success",
                        )
                        md = _metadata_from_dict(event["metadata"])
                        recorder.record_write(md, is_new=event["is_new"])
                    elif op == "delete":
                        op_logger.log_operation(
                            operation_type="delete",
                            path=event["path"],
                            zone_id=zone_id,
                            agent_id=agent_id,
                            snapshot_hash=event.get("snapshot_hash"),
                            metadata_snapshot=event.get("metadata_snapshot"),
                            status="success",
                        )
                        recorder.record_delete(event["path"])
                    elif op == "rename":
                        op_logger.log_operation(
                            operation_type="rename",
                            path=event["path"],
                            new_path=event.get("new_path"),
                            zone_id=zone_id,
                            agent_id=agent_id,
                            snapshot_hash=event.get("snapshot_hash"),
                            metadata_snapshot=event.get("metadata_snapshot"),
                            status="success",
                        )
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

                session.commit()

            self._total_flushed += len(events)
            return len(events)
        except Exception as e:
            self._total_failed += len(events)
            logger.error("PipedRecordStoreWriteObserver pre-buffer flush failed: %s", e)
            return 0

    async def stop(self) -> None:
        """Graceful shutdown: signal pipe closed, drain remaining events, then stop."""
        if self._consumer_task is not None and not self._consumer_task.done():
            # Signal close — wakes blocked consumer, allows drain of remaining messages
            if self._pipe_manager is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._pipe_manager.signal_close(_AUDIT_PIPE_PATH)

            # Let consumer drain naturally, with timeout
            try:
                await asyncio.wait_for(asyncio.shield(self._consumer_task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._consumer_task

            self._consumer_task = None
        self._pipe_ready = False

        # Drain any residual _pre_buffer events (CLI mode or events that arrived
        # after pipe closed but before enqueue path switched off)
        self.flush_sync()

    def flush_sync(self) -> int:
        """Synchronously drain ``_pre_buffer`` directly to the DB.

        Used by CLI shutdown path (NexusFS.close) where no asyncio loop
        is running and PipeManager was never injected. Returns count of
        flushed events.
        """
        if not self._pre_buffer:
            return 0

        events = [json.loads(data) for data in self._pre_buffer]
        self._pre_buffer.clear()

        try:
            with self._session_factory() as session:
                self._process_events_in_session(session, events)
                session.commit()
            count = len(events)
            self._total_flushed += count
            logger.debug("flush_sync: flushed %d pre-buffer events", count)
            return count
        except Exception as e:
            logger.error("flush_sync failed, %d events lost: %s", len(events), e)
            self._total_failed += len(events)
            return 0

    # ------------------------------------------------------------------
    # Background consumer
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Background consumer: read from pipe, batch, flush to RecordStore."""
        from nexus.core.pipe import PipeClosedError, PipeEmptyError, PipeNotFoundError

        assert self._pipe_manager is not None

        pipe_mgr = self._pipe_manager
        while True:
            # Block until first event arrives
            try:
                first = await pipe_mgr.pipe_read(_AUDIT_PIPE_PATH)
            except (PipeClosedError, PipeNotFoundError):
                logger.debug("Audit pipe closed, consumer exiting")
                break

            # Drain available events for batching
            batch: list[dict[str, Any]] = [json.loads(first)]
            for _ in range(_MAX_BATCH_DRAIN - 1):
                try:
                    data = await pipe_mgr.pipe_read(_AUDIT_PIPE_PATH, blocking=False)
                    batch.append(json.loads(data))
                except (PipeEmptyError, PipeClosedError, PipeNotFoundError):
                    break

            await self._flush_batch(batch)

    @staticmethod
    def _build_urn(path: str, zone_id: str | None) -> str:
        """Build a locator URN for a file from its virtual path.

        Delegates to NexusURN.for_file() — single source of truth for
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
        The begin_nested() savepoint isolates MCL errors, but internal
        retry logic (e.g. MCLRecorder._next_sequence_fallback) can issue
        queries after savepoint rollback, causing "closed transaction"
        errors that escape the context manager.  The outer try/except
        catches these to protect file_paths + version_history writes.
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
                    # URNs are locators (Issue #2929 Key Decision #3):
                    # rename changes the URN. DELETE old + UPSERT new.
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

        Shared by ``_flush_batch()`` (async consumer) and ``flush_sync()`` (CLI drain).
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
                # Soft-delete entity aspects (Issue #2929)
                from nexus.storage.aspect_service import AspectService

                AspectService(session).soft_delete_entity_aspects(urn)

            elif op == "rename":
                # Two rows: DELETE old + UPSERT new (locator URNs)
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
                    recorder.record_rename(event["path"], new_path)

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

            # MCL recording moved to _flush_batch_sync Phase 2 (separate session)
            # to prevent MCL failures from corrupting critical writes.

    def _flush_batch_sync(self, events: list[dict[str, Any]]) -> None:
        """Synchronous flush: critical writes first, MCL second.

        Phase 1 commits operation_log + file_paths + version_history.
        Phase 2 records MCL entries in a separate session so failures
        (e.g. sequence_number issues) cannot corrupt the critical writes.
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
        # Session creation is inside the try so that a factory failure
        # is caught here (non-critical) instead of propagating to
        # _flush_batch, which would retry the whole batch after Phase 1
        # already committed — duplicating operation_log / version-history rows.
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

    async def _flush_batch(self, events: list[dict[str, Any]], attempt: int = 0) -> None:
        """Flush a batch of events to RecordStore in a single transaction."""
        t0 = time.monotonic()
        try:
            self._flush_batch_sync(events)

            duration = time.monotonic() - t0
            self._total_flushed += len(events)
            logger.info(
                "[PIPE] Flushed %d events in %.3fs",
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
                    "PipedRecordStoreWriteObserver flush failed "
                    "(attempt %d/%d, retry in %.1fs): %s",
                    attempt + 1,
                    _MAX_RETRIES,
                    wait,
                    e,
                )
                await asyncio.sleep(wait)
                await self._flush_batch(events, attempt=attempt + 1)
            else:
                self._total_failed += len(events)
                logger.error(
                    "PipedRecordStoreWriteObserver flush FAILED after %d retries, "
                    "dropping %d events: %s",
                    _MAX_RETRIES,
                    len(events),
                    e,
                )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> dict[str, int]:
        """Return observer metrics."""
        return {
            "total_enqueued": self._total_enqueued,
            "total_flushed": self._total_flushed,
            "total_failed": self._total_failed,
            "total_retries": self._total_retries,
            "total_dropped": self._total_dropped,
            "pre_buffer_size": len(self._pre_buffer),
        }
