"""EventDeliveryWorker — transactional outbox delivery (Issue #1241).

Polls ``operation_log`` rows where ``delivered = FALSE``, builds
``FileEvent`` objects from the existing columns, dispatches them to
downstream systems (EventBus, webhooks, hooks, exporters), and marks
them ``delivered = TRUE`` in a single transaction.

Key guarantees:
- **At-least-once delivery**: events are only marked delivered after
  successful dispatch.  On crash, undelivered rows are retried.
- **Concurrent safety**: ``SELECT ... FOR UPDATE SKIP LOCKED`` prevents
  two workers from processing the same batch.
- **Backpressure**: exponential backoff on consecutive empty polls
  (200ms -> 400ms -> 800ms -> cap) to reduce idle DB pressure.
- **DLQ routing**: events failing after max_retries are sent to the
  dead letter queue (Issue #1138).

Tracked by: Issue #1241, #1138
"""

import asyncio
import itertools
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.operation_types import OperationType
from nexus.system_services.event_bus.types import FileEvent, FileEventType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.record_store import RecordStoreABC
    from nexus.system_services.event_log.exporter_registry import ExporterRegistry

logger = logging.getLogger(__name__)

# ---- Mapping: operation_type -> FileEventType --------------------------------

_OP_TO_EVENT_TYPE: dict[str, FileEventType] = {
    OperationType.WRITE: FileEventType.FILE_WRITE,
    OperationType.DELETE: FileEventType.FILE_DELETE,
    OperationType.RENAME: FileEventType.FILE_RENAME,
    OperationType.MKDIR: FileEventType.DIR_CREATE,
    OperationType.RMDIR: FileEventType.DIR_DELETE,
    OperationType.CHMOD: FileEventType.METADATA_CHANGE,
    OperationType.CHOWN: FileEventType.METADATA_CHANGE,
    OperationType.CHGRP: FileEventType.METADATA_CHANGE,
    OperationType.SETFACL: FileEventType.METADATA_CHANGE,
}

# ---- Sync -> async bridge helper -------------------------------------------


def _run_async(coro: Any, loop: asyncio.AbstractEventLoop | None = None) -> Any:
    """Run an async coroutine from a sync context, properly awaiting the result.

    If an event loop is provided and running, schedules via
    run_coroutine_threadsafe and *awaits* the Future (fixes fire-and-forget bug).
    Otherwise creates a temporary loop with asyncio.run().
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        # Block until coroutine completes (fixes fire-and-forget bug)
        return future.result(timeout=30.0)
    else:
        return asyncio.run(coro)


class EventDeliveryWorker:
    """Background worker polling undelivered events from operation_log.

    Uses ``SELECT FOR UPDATE SKIP LOCKED`` for concurrent worker safety.
    Dispatches to EventBus (Redis Pub/Sub), webhooks, hooks, and external
    exporters (Kafka, NATS, Pub/Sub via ExporterRegistry).
    Marks events ``delivered = TRUE`` after successful dispatch.
    Routes failures to DLQ after max_retries.
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        event_bus: Any | None = None,
        exporter_registry: "ExporterRegistry | None" = None,
        subscription_manager_getter: Callable[[], Any] | None = None,
        *,
        event_loop: asyncio.AbstractEventLoop | None = None,
        poll_interval_ms: int = 200,
        batch_size: int = 50,
        max_retries: int = 3,
        max_backoff_ms: int = 5000,
        use_row_locking: bool = False,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._event_bus = event_bus
        self._exporter_registry = exporter_registry
        self._subscription_manager_getter = subscription_manager_getter
        self._event_loop = event_loop
        self._poll_interval_s = poll_interval_ms / 1000.0
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._max_backoff_s = max_backoff_ms / 1000.0
        self._use_row_locking = use_row_locking

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._consecutive_empty = 0

        # Metrics
        self._total_dispatched = 0
        self._total_failed = 0
        self._total_dlq = 0

    # ---- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start background polling thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("EventDeliveryWorker already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="event-delivery-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "EventDeliveryWorker started (poll=%.0fms, batch=%d, exporters=%s)",
            self._poll_interval_s * 1000,
            self._batch_size,
            self._exporter_registry.exporter_names if self._exporter_registry else "none",
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Graceful shutdown: signal stop, then wait for in-flight work."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("EventDeliveryWorker did not stop within %.1fs", timeout)
            self._thread = None
        logger.info(
            "EventDeliveryWorker stopped (dispatched=%d, failed=%d, dlq=%d)",
            self._total_dispatched,
            self._total_failed,
            self._total_dlq,
        )

    @property
    def metrics(self) -> dict[str, int]:
        return {
            "total_dispatched": self._total_dispatched,
            "total_failed": self._total_failed,
            "total_dlq": self._total_dlq,
            "consecutive_empty": self._consecutive_empty,
        }

    # ---- Main loop -----------------------------------------------------------

    def _run_loop(self) -> None:
        """Poll loop with exponential backoff on empty results."""
        while not self._stop_event.is_set():
            try:
                count = self._poll_and_dispatch()
                if count > 0:
                    self._consecutive_empty = 0
                else:
                    self._consecutive_empty += 1
            except Exception:
                logger.exception("EventDeliveryWorker poll error")
                self._consecutive_empty += 1

            # Backoff: base interval * 2^(consecutive_empty), capped
            backoff = min(
                self._poll_interval_s * (2 ** min(self._consecutive_empty, 10)),
                self._max_backoff_s,
            )
            self._stop_event.wait(backoff)

    # ---- Core: poll -> dispatch -> mark --------------------------------------

    def _poll_and_dispatch(self) -> int:
        """Poll undelivered rows, dispatch events, mark delivered.

        Returns:
            Number of events successfully dispatched.
        """
        from sqlalchemy import select

        from nexus.storage.models.operation_log import OperationLogModel

        dispatched_ids: list[str] = []

        with self._session_factory() as session:
            # Build SELECT ... WHERE delivered = FALSE ORDER BY sequence_number
            # FOR UPDATE SKIP LOCKED (PG) or plain SELECT (SQLite)
            # sequence_number enforces causal ordering (#2755).
            stmt = (
                select(OperationLogModel)
                .where(OperationLogModel.delivered == False)  # noqa: E712
                .order_by(OperationLogModel.sequence_number)
                .limit(self._batch_size)
            )

            # Row-level locking for concurrent workers (PostgreSQL)
            if self._use_row_locking:
                stmt = stmt.with_for_update(skip_locked=True)

            rows = list(session.execute(stmt).scalars())

            if not rows:
                return 0

            # Build FileEvents for the batch
            events_with_records: list[tuple[FileEvent, Any]] = []
            for record in rows:
                events_with_records.append((self._build_file_event(record), record))

            # 1. Dispatch events to EventBus + webhooks, ordered per zone (#2755).
            #    Group by zone_id and dispatch sequentially within each zone
            #    (parallel across zones is safe — no cross-zone causal deps).
            def zone_key(pair: tuple[FileEvent, Any]) -> str:
                return pair[1].zone_id or ROOT_ZONE_ID

            sorted_pairs = sorted(events_with_records, key=zone_key)
            for _zone_id, zone_group in itertools.groupby(sorted_pairs, key=zone_key):
                for event, record in zone_group:
                    try:
                        self._dispatch_event_internal(event, record)
                        dispatched_ids.append(record.operation_id)
                        self._total_dispatched += 1
                    except Exception as exc:
                        self._handle_dispatch_failure(session, record, event, exc)

            # 2. Dispatch batch to external exporters (parallel)
            if self._exporter_registry and self._exporter_registry.exporter_names:
                events = [ev for ev, _ in events_with_records]
                self._dispatch_to_exporters(session, events, events_with_records)

            # Mark successfully dispatched rows as delivered
            if dispatched_ids:
                self._mark_delivered(session, dispatched_ids)

            # Commit: delivered marks + any DLQ entries added during this batch
            session.commit()

        return len(dispatched_ids)

    def _dispatch_event_internal(self, event: FileEvent, record: Any) -> None:
        """Dispatch event to EventBus and webhook subscriptions."""
        # 1. Publish to EventBus (Redis Pub/Sub)
        bus = self._event_bus
        if bus is not None:
            _run_async(bus.publish(event), self._event_loop)

        # 2. Broadcast to webhook subscriptions (injected getter, no server import)
        getter = self._subscription_manager_getter
        if getter is not None:
            sub_manager = getter()
            if sub_manager is not None:
                event_type_str = str(event.type)

                async def _broadcast() -> None:
                    await sub_manager.broadcast(
                        event_type=event_type_str,
                        data={
                            "file_path": event.path,
                            "old_path": event.old_path,
                            "size": event.size,
                            "timestamp": event.timestamp,
                            "sequence_number": event.sequence_number,
                        },
                        zone_id=event.zone_id or ROOT_ZONE_ID,
                    )

                _run_async(_broadcast(), self._event_loop)

        logger.debug(
            "[DELIVERY] Dispatched: %s %s (op=%s)",
            event.type,
            event.path,
            record.operation_id,
        )

    def _handle_dispatch_failure(
        self, session: "Session", record: Any, event: FileEvent, exc: Exception
    ) -> None:
        """Handle a failed dispatch: increment persistent retry_count or route to DLQ.

        Uses ``record.retry_count`` (persisted in DB) instead of an in-memory
        dict so retry state survives worker restarts (Issue #2751).
        """
        op_id = record.operation_id
        record.retry_count = record.retry_count + 1
        retries = record.retry_count

        if retries >= self._max_retries:
            # Route to DLQ and mark delivered to stop re-polling
            try:
                from nexus.system_services.event_log.dead_letter import DeadLetterHandler

                handler = DeadLetterHandler()
                handler.route_to_dlq(
                    session,
                    operation_id=op_id,
                    exporter_name="internal",
                    error=exc,
                    event=event,
                    retry_count=retries,
                )
                record.delivered = True
                self._total_dlq += 1
            except Exception:
                logger.exception("Failed to route event %s to DLQ", op_id)
        else:
            logger.warning(
                "Dispatch failed for %s (retry %d/%d): %s",
                op_id,
                retries,
                self._max_retries,
                exc,
            )
        self._total_failed += 1

    def _dispatch_to_exporters(
        self,
        session: "Session",
        events: list[FileEvent],
        events_with_records: list[tuple[FileEvent, Any]],
    ) -> None:
        """Dispatch event batch to external exporters via ExporterRegistry."""
        registry = self._exporter_registry
        if not registry:
            return

        try:
            failures = _run_async(registry.dispatch_batch(events), self._event_loop)
        except Exception:
            logger.exception("ExporterRegistry batch dispatch failed")
            return

        # Route per-exporter failures to DLQ
        if failures:
            from nexus.system_services.event_log.dead_letter import DeadLetterHandler

            handler = DeadLetterHandler()
            # Build event_id -> (event, record) map
            id_map = {ev.event_id: (ev, rec) for ev, rec in events_with_records}

            for exporter_name, failed_ids in failures.items():
                for event_id in failed_ids:
                    pair = id_map.get(event_id)
                    if pair:
                        event, record = pair
                        handler.route_to_dlq(
                            session,
                            operation_id=record.operation_id,
                            exporter_name=exporter_name,
                            error=ConnectionError(f"Export to {exporter_name} failed"),
                            event=event,
                        )
                        self._total_dlq += 1

    def _build_file_event(self, record: Any) -> FileEvent:
        """Build a FileEvent from an OperationLogModel record."""
        event_type = _OP_TO_EVENT_TYPE.get(record.operation_type, record.operation_type)

        return FileEvent(
            type=event_type,
            path=record.path,
            zone_id=record.zone_id or ROOT_ZONE_ID,
            timestamp=record.created_at.isoformat() if record.created_at else "",
            old_path=record.new_path,  # new_path stores old_path for renames
            agent_id=record.agent_id,
            sequence_number=record.sequence_number,
        )

    def _mark_delivered(self, session: "Session", operation_ids: list[str]) -> None:
        """Mark operation_log rows as delivered."""
        from sqlalchemy import update

        from nexus.storage.models.operation_log import OperationLogModel

        session.execute(
            update(OperationLogModel)
            .where(OperationLogModel.operation_id.in_(operation_ids))
            .values(delivered=True)
        )
