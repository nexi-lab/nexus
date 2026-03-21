"""DT_PIPE consumer — async RecordStore sync via kernel IPC.

Pure consumer: reads audit events from a DT_PIPE via ``nx.sys_read()``
and flushes them to RecordStore in batches.  The producer side is
``AuditWriteInterceptor`` which writes events via ``nx.sys_write()``.

Issue #809: Decouple write_observer.on_write() sync DB write from hot path.
Issue #1772: Migrate from PipeManager to sys_write/sys_read.

Architecture:
    AuditWriteInterceptor (async POST hook)
      -> JSON serialize -> nx.sys_write(pipe_path)  # ~1μs via fast-path

    PipedRecordStoreWriteObserver (background consumer)
      -> nx.sys_read(pipe_path) (async, blocking)
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

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

# Pipe path and capacity (parallel to WorkflowDispatchService constants)
_AUDIT_PIPE_PATH = "/nexus/pipes/audit-events"
_AUDIT_PIPE_CAPACITY = 65_536  # 64KB

# Consumer batch processing
_MAX_BATCH_DRAIN = 100  # Max events to drain per batch
_MAX_RETRIES = 3


def _metadata_from_dict(d: dict[str, Any]) -> Any:
    """Reconstruct FileMetadata from to_dict() output."""
    from nexus.contracts.metadata import FileMetadata

    if d.get("created_at") and isinstance(d["created_at"], str):
        d["created_at"] = datetime.fromisoformat(d["created_at"])
    if d.get("modified_at") and isinstance(d["modified_at"], str):
        d["modified_at"] = datetime.fromisoformat(d["modified_at"])
    return FileMetadata(**d)


class PipedRecordStoreWriteObserver:
    """DT_PIPE consumer for async RecordStore sync.

    Pure consumer — reads audit events from a DT_PIPE via ``nx.sys_read()``
    and flushes them to RecordStore (OperationLogger + VersionRecorder).
    The producer is ``AuditWriteInterceptor``.

    Lifecycle:
        1. Created in factory (system tier) with record_store
        2. NexusFS bound via bind_fs() (deferred)
        3. start() creates pipe via sys_setattr, spawns consumer
        4. stop() cancels consumer, flushes remaining events
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        *,
        strict_mode: bool = True,
        event_signal: "asyncio.Event | None" = None,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._strict_mode = strict_mode
        self._event_signal = event_signal  # Issue #3193: wake delivery worker

        # NexusFS reference (deferred injection)
        self._nx: NexusFS | None = None
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

    def bind_fs(self, nx: "NexusFS") -> None:
        """Bind NexusFS for sys_read/sys_write pipe access."""
        self._nx = nx

    def register_post_flush_hook(self, hook: Any) -> None:
        """Register a callback invoked after each successful flush.

        Hooks receive the list of flushed events. They run AFTER the
        audit trail commit, so failures do not block the audit path.
        Used by CatalogService for async-on-write extraction (Issue #2978).
        """
        self._post_flush_hooks.append(hook)

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create audit pipe via sys_setattr, spawn consumer."""
        if self._pipe_ready:
            return

        if self._nx is None:
            return  # CLI mode — no NexusFS

        from nexus.contracts.metadata import DT_PIPE

        with contextlib.suppress(Exception):
            await self._nx.sys_setattr(
                _AUDIT_PIPE_PATH,
                entry_type=DT_PIPE,
                owner_id="kernel",
            )

        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())

    async def flush(self, timeout: float = 5.0) -> int:
        """Drain the pipe and flush all pending events to RecordStore.

        Blocks until all enqueued events have been committed to the database,
        ensuring that subsequent queries (e.g. list_versions) see the data.

        Args:
            timeout: Maximum seconds to wait for events to appear. Defaults to 5.

        Returns:
            Number of events flushed.
        """
        if not self._pipe_ready or self._nx is None:
            # Pipe not started — flush pre-buffer directly via sync path
            return self._flush_pre_buffer_sync()

        from nexus.contracts.exceptions import NexusFileNotFoundError

        # Drain all available events from the pipe (non-blocking)
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                data = await self._nx.sys_read(_AUDIT_PIPE_PATH)
                batch.append(json.loads(data))
            except (NexusFileNotFoundError, Exception):
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
            # Signal close — wakes blocked consumer via sys_unlink
            if self._nx is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    await self._nx.sys_unlink(_AUDIT_PIPE_PATH)

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
        """Background consumer: read from pipe via sys_read, batch, flush."""
        from nexus.contracts.exceptions import NexusFileNotFoundError
        from nexus.core.pipe import PipeClosedError, PipeEmptyError

        assert self._nx is not None

        nx = self._nx
        pm = nx._pipe_manager
        while True:
            # Block until first event arrives
            try:
                first = await nx.sys_read(_AUDIT_PIPE_PATH)
            except NexusFileNotFoundError:
                logger.debug("Audit pipe closed, consumer exiting")
                break

            # Drain available events for batching (non-blocking read_nowait)
            batch: list[dict[str, Any]] = [json.loads(first)]
            if pm is not None:
                buf = pm._buffers.get(_AUDIT_PIPE_PATH)
                if buf is not None:
                    for _ in range(_MAX_BATCH_DRAIN - 1):
                        try:
                            data = buf.read_nowait()
                            batch.append(json.loads(data))
                        except (PipeEmptyError, PipeClosedError, Exception):
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

            # Issue #3193: signal delivery worker immediately after commit
            if self._event_signal is not None:
                self._event_signal.set()

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
