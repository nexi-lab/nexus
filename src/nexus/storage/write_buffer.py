"""Write-behind buffer for asynchronous PostgreSQL materialized view sync.

Decouples Raft writes from PostgreSQL writes so the hot path returns immediately.
Events are buffered in memory and flushed to PostgreSQL periodically in batches.

Issue #1246: Implements the post-commit retry mechanism (Decision 4B, 13A).

Architecture:
    Raft commit → kernel returns immediately
    ↓ (async)
    WriteBuffer collects events in memory queue
    ↓ (background thread, every flush_interval_ms or max_buffer_size)
    Flush → single PostgreSQL transaction (batch upsert)
    ↓ (on failure)
    Retry with exponential backoff, events preserved

Usage:
    buffer = WriteBuffer(session_factory, flush_interval_ms=100, max_buffer_size=100)
    buffer.start()

    # Hot path — returns immediately
    buffer.enqueue_write(metadata, is_new=True, path="/file.txt", zone_id="default")

    # Graceful shutdown — drains remaining events
    buffer.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of write events that can be buffered."""

    WRITE = "write"
    DELETE = "delete"
    RENAME = "rename"


@dataclass(frozen=True)
class WriteEvent:
    """Immutable write event queued for PostgreSQL sync."""

    event_type: EventType
    path: str
    timestamp: float = field(default_factory=time.monotonic)

    # Write-specific
    metadata: Any = None
    is_new: bool = False
    zone_id: str | None = None
    agent_id: str | None = None
    snapshot_hash: str | None = None
    metadata_snapshot: dict[str, Any] | None = None

    # Rename-specific
    old_path: str | None = None
    new_path: str | None = None


class WriteBuffer:
    """Thread-safe write-behind buffer for PostgreSQL materialized view sync.

    Collects write events and flushes them in periodic batches to reduce
    per-write PostgreSQL overhead from ~2-10ms to amortized ~0.1ms.
    """

    def __init__(
        self,
        session_factory: Callable[..., Any],
        *,
        flush_interval_ms: int = 100,
        max_buffer_size: int = 100,
        max_retries: int = 3,
    ) -> None:
        """Initialize the write buffer.

        Args:
            session_factory: SQLAlchemy session factory (from RecordStore).
            flush_interval_ms: Flush interval in milliseconds.
            max_buffer_size: Flush when buffer reaches this many events.
            max_retries: Max retries on flush failure before dropping events.
        """
        self._session_factory = session_factory
        self._flush_interval = flush_interval_ms / 1000.0  # convert to seconds
        self._max_buffer_size = max_buffer_size
        self._max_retries = max_retries

        self._buffer: list[WriteEvent] = []
        self._lock = threading.Lock()
        self._flush_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Metrics
        self._total_enqueued = 0
        self._total_flushed = 0
        self._total_failed = 0
        self._total_retries = 0
        self._flush_count = 0
        self._flush_duration_sum = 0.0
        self._flush_batch_size_sum = 0
        self._enqueued_by_type: dict[str, int] = {"write": 0, "delete": 0, "rename": 0}

    def start(self) -> None:
        """Start the background flush thread."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._flush_loop,
            name="write-buffer-flush",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "WriteBuffer started (flush_interval=%dms, max_buffer=%d)",
            int(self._flush_interval * 1000),
            self._max_buffer_size,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the flush thread and drain remaining events.

        Args:
            timeout: Max seconds to wait for drain.
        """
        self._stop_event.set()
        self._flush_event.set()  # Wake the flush loop

        if self._thread is not None:
            self._thread.join(timeout=timeout)

        # Final drain
        self._flush_buffer()

        logger.info(
            "WriteBuffer stopped (enqueued=%d, flushed=%d, failed=%d)",
            self._total_enqueued,
            self._total_flushed,
            self._total_failed,
        )

    @property
    def pending_count(self) -> int:
        """Number of events waiting to be flushed."""
        with self._lock:
            return len(self._buffer)

    @property
    def metrics(self) -> dict[str, int | float | dict[str, int]]:
        """Return a consistent snapshot of buffer metrics."""
        with self._lock:
            return {
                "total_enqueued": self._total_enqueued,
                "total_flushed": self._total_flushed,
                "total_failed": self._total_failed,
                "total_retries": self._total_retries,
                "pending": len(self._buffer),
                "flush_count": self._flush_count,
                "flush_duration_sum": self._flush_duration_sum,
                "flush_batch_size_sum": self._flush_batch_size_sum,
                "enqueued_by_type": dict(self._enqueued_by_type),
            }

    # -- Enqueue methods (called from hot path) ----------------------------

    def enqueue_write(
        self,
        metadata: Any,
        *,
        is_new: bool,
        path: str,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a write event. Returns immediately."""
        event = WriteEvent(
            event_type=EventType.WRITE,
            path=path,
            metadata=metadata,
            is_new=is_new,
            zone_id=zone_id,
            agent_id=agent_id,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=metadata_snapshot,
        )
        self._enqueue(event)

    def enqueue_delete(
        self,
        path: str,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        snapshot_hash: str | None = None,
        metadata_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a delete event. Returns immediately."""
        event = WriteEvent(
            event_type=EventType.DELETE,
            path=path,
            zone_id=zone_id,
            agent_id=agent_id,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=metadata_snapshot,
        )
        self._enqueue(event)

    def enqueue_rename(
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
        event = WriteEvent(
            event_type=EventType.RENAME,
            path=old_path,
            old_path=old_path,
            new_path=new_path,
            zone_id=zone_id,
            agent_id=agent_id,
            snapshot_hash=snapshot_hash,
            metadata_snapshot=metadata_snapshot,
        )
        self._enqueue(event)

    # -- Internal ----------------------------------------------------------

    def _enqueue(self, event: WriteEvent) -> None:
        """Add event to buffer, trigger flush if threshold reached."""
        with self._lock:
            self._buffer.append(event)
            self._total_enqueued += 1
            self._enqueued_by_type[event.event_type.value] = (
                self._enqueued_by_type.get(event.event_type.value, 0) + 1
            )
            buffer_size = len(self._buffer)

        if buffer_size >= self._max_buffer_size:
            self._flush_event.set()

    def _flush_loop(self) -> None:
        """Background loop: flush buffer periodically or on threshold."""
        while not self._stop_event.is_set():
            self._flush_event.wait(timeout=self._flush_interval)
            self._flush_event.clear()
            self._flush_buffer()

    def _flush_buffer(self) -> None:
        """Drain and process all buffered events in a single transaction."""
        with self._lock:
            if not self._buffer:
                return
            events = list(self._buffer)
            self._buffer.clear()

        self._process_events(events)

    def _process_events(self, events: list[WriteEvent], attempt: int = 0) -> None:
        """Process a batch of events with retry on failure."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.version_recorder import VersionRecorder

        t0 = time.monotonic()
        try:
            with self._session_factory() as session:
                op_logger = OperationLogger(session)
                recorder = VersionRecorder(session)

                for event in events:
                    zone = event.zone_id or "default"

                    if event.event_type == EventType.WRITE:
                        op_logger.log_operation(
                            operation_type="write",
                            path=event.path,
                            zone_id=zone,
                            agent_id=event.agent_id,
                            snapshot_hash=event.snapshot_hash,
                            metadata_snapshot=event.metadata_snapshot,
                            status="success",
                        )
                        recorder.record_write(event.metadata, is_new=event.is_new)

                    elif event.event_type == EventType.DELETE:
                        op_logger.log_operation(
                            operation_type="delete",
                            path=event.path,
                            zone_id=zone,
                            agent_id=event.agent_id,
                            snapshot_hash=event.snapshot_hash,
                            metadata_snapshot=event.metadata_snapshot,
                            status="success",
                        )
                        recorder.record_delete(event.path)

                    elif event.event_type == EventType.RENAME:
                        op_logger.log_operation(
                            operation_type="rename",
                            path=event.path,
                            new_path=event.new_path,
                            zone_id=zone,
                            agent_id=event.agent_id,
                            snapshot_hash=event.snapshot_hash,
                            metadata_snapshot=event.metadata_snapshot,
                            status="success",
                        )

                session.commit()
                duration = time.monotonic() - t0
                with self._lock:
                    self._total_flushed += len(events)
                    self._flush_count += 1
                    self._flush_duration_sum += duration
                    self._flush_batch_size_sum += len(events)
                logger.debug("WriteBuffer flushed %d events in %.3fs", len(events), duration)

        except Exception as e:
            if attempt < self._max_retries:
                with self._lock:
                    self._total_retries += 1
                wait = 0.1 * (2**attempt)  # 100ms, 200ms, 400ms
                logger.warning(
                    "WriteBuffer flush failed (attempt %d/%d, retry in %.1fs): %s",
                    attempt + 1,
                    self._max_retries,
                    wait,
                    e,
                )
                time.sleep(wait)
                self._process_events(events, attempt=attempt + 1)
            else:
                with self._lock:
                    self._total_failed += len(events)
                logger.error(
                    "WriteBuffer flush FAILED after %d retries, dropping %d events: %s",
                    self._max_retries,
                    len(events),
                    e,
                )
