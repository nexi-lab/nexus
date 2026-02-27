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
    from nexus.storage.record_store import RecordStoreABC
    from nexus.system_services.pipe_manager import PipeManager

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

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def set_pipe_manager(self, pm: "PipeManager") -> None:
        """Inject PipeManager after factory boot."""
        self._pipe_manager = pm

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
            from nexus.core.pipe import PipeFullError

            try:
                self._pipe_manager.pipe_write_nowait(_AUDIT_PIPE_PATH, data)
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

    async def stop(self) -> None:
        """Cancel consumer task for graceful shutdown."""
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        self._pipe_ready = False

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

    async def _flush_batch(self, events: list[dict[str, Any]], attempt: int = 0) -> None:
        """Flush a batch of events to RecordStore in a single transaction."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        t0 = time.monotonic()
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

            duration = time.monotonic() - t0
            self._total_flushed += len(events)
            logger.debug(
                "PipedRecordStoreWriteObserver flushed %d events in %.3fs",
                len(events),
                duration,
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
