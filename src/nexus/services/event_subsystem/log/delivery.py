"""EventDeliveryWorker — transactional outbox delivery (Issue #1241, #3193).

Awaits an ``asyncio.Event`` notification from PipedRecordStoreWriteObserver
(fired after each successful operation_log commit), then drains all
``delivered = FALSE`` rows, dispatches them to downstream systems, and
marks them ``delivered = TRUE``.

Replaces the previous polling loop (200 ms → 5 s exponential backoff) with
event-driven wakeup (~µs latency, zero idle DB queries).

Key guarantees:
- **At-least-once delivery**: events are only marked delivered after
  successful dispatch.  On crash, undelivered rows are retried.
- **Concurrent safety**: ``SELECT ... FOR UPDATE SKIP LOCKED`` prevents
  two workers from processing the same batch.
- **Drain-then-wait**: after processing a batch, immediately re-query;
  only ``await event.wait()`` when no undelivered rows remain.
- **DLQ routing**: events failing after max_retries are sent to the
  dead letter queue (Issue #1138).

Tracked by: Issue #1241, #1138, #3193
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.operation_types import OperationType
from nexus.services.event_subsystem.types import FileEvent, FileEventType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.services.event_subsystem.log.exporter_registry import ExporterRegistry
    from nexus.storage.record_store import RecordStoreABC

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


class EventDeliveryWorker:
    """Async background worker delivering undelivered events from operation_log.

    Uses ``asyncio.Event`` notification from the write observer for µs wakeup
    instead of polling.  Falls back to a periodic sweep if no event signal is
    provided (backward-compatible).

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
        event_signal: asyncio.Event | None = None,
        batch_size: int = 50,
        max_retries: int = 3,
        fallback_poll_interval_s: float = 5.0,
        use_row_locking: bool = False,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._event_bus = event_bus
        self._exporter_registry = exporter_registry
        self._subscription_manager_getter = subscription_manager_getter
        self._event_signal = event_signal
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._fallback_poll_interval_s = fallback_poll_interval_s
        self._use_row_locking = use_row_locking

        self._consumer_task: asyncio.Task[None] | None = None
        self._stopped = False

        # In-memory retry tracking: operation_id -> retry count
        self._retry_counts: dict[str, int] = {}

        # Metrics
        self._total_dispatched = 0
        self._total_failed = 0
        self._total_dlq = 0

    # ---- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start background consumer task."""
        if self._consumer_task is not None and not self._consumer_task.done():
            logger.warning("EventDeliveryWorker already running")
            return

        self._stopped = False
        self._consumer_task = asyncio.create_task(self._consume())
        logger.info(
            "EventDeliveryWorker started (batch=%d, signal=%s, exporters=%s)",
            self._batch_size,
            "yes" if self._event_signal else "fallback-poll",
            self._exporter_registry.exporter_names if self._exporter_registry else "none",
        )

    async def stop(self) -> None:
        """Graceful shutdown: cancel task and wait for in-flight work."""
        self._stopped = True
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
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
        }

    # ---- Main loop (Issue #3193) ---------------------------------------------

    async def _consume(self) -> None:
        """Drain-then-wait loop: process all undelivered rows, then await signal.

        Pattern (clear-then-check-then-wait) prevents lost wakeups::

            while not stopped:
                event.clear()          # clear BEFORE checking DB
                count = poll_and_dispatch()
                if count == 0:
                    await event.wait() # block until next notification
        """
        signal = self._event_signal

        while not self._stopped:
            try:
                # Clear BEFORE checking DB to prevent lost wakeup
                if signal is not None:
                    signal.clear()

                # Drain all available undelivered rows
                count = await self._poll_and_dispatch()

                if count > 0:
                    # More rows may exist — loop immediately
                    continue

                # No rows: wait for notification or fallback poll
                if signal is not None:
                    await signal.wait()
                else:
                    await asyncio.sleep(self._fallback_poll_interval_s)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EventDeliveryWorker consume error")
                # Brief back-off after unexpected error
                await asyncio.sleep(1.0)

    # ---- Core: poll -> dispatch -> mark --------------------------------------

    async def _poll_and_dispatch(self) -> int:
        """Poll undelivered rows, dispatch events, mark delivered.

        Returns:
            Number of events successfully dispatched.
        """
        from sqlalchemy import select

        from nexus.storage.models.operation_log import OperationLogModel

        dispatched_ids: list[str] = []

        with self._session_factory() as session:
            # Build SELECT ... WHERE delivered = FALSE ORDER BY created_at
            # FOR UPDATE SKIP LOCKED (PG) or plain SELECT (SQLite)
            stmt = (
                select(OperationLogModel)
                .where(OperationLogModel.delivered == False)  # noqa: E712
                .order_by(OperationLogModel.created_at)
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

            # 1. Dispatch each event to EventBus + webhooks
            for event, record in events_with_records:
                try:
                    await self._dispatch_event_internal(event, record)
                    dispatched_ids.append(record.operation_id)
                    self._total_dispatched += 1
                    # Clear retry count on success
                    self._retry_counts.pop(record.operation_id, None)
                except Exception as exc:
                    self._handle_dispatch_failure(session, record, event, exc)

            # 2. Dispatch batch to external exporters (parallel)
            if self._exporter_registry and self._exporter_registry.exporter_names:
                events = [ev for ev, _ in events_with_records]
                await self._dispatch_to_exporters(session, events, events_with_records)

            # Mark successfully dispatched rows as delivered
            if dispatched_ids:
                self._mark_delivered(session, dispatched_ids)

            # Commit: delivered marks + any DLQ entries added during this batch
            session.commit()

        return len(dispatched_ids)

    async def _dispatch_event_internal(self, event: FileEvent, record: Any) -> None:
        """Dispatch event to EventBus and webhook subscriptions."""
        # 1. Publish to EventBus (Redis Pub/Sub)
        bus = self._event_bus
        if bus is not None:
            await bus.publish(event)

        # 2. Broadcast to webhook subscriptions (injected getter, no server import)
        getter = self._subscription_manager_getter
        if getter is not None:
            sub_manager = getter()
            if sub_manager is not None:
                await sub_manager.broadcast(
                    event_type=str(event.type),
                    data={
                        "file_path": event.path,
                        "old_path": event.old_path,
                        "size": event.size,
                        "timestamp": event.timestamp,
                    },
                    zone_id=event.zone_id or ROOT_ZONE_ID,
                )

        logger.debug(
            "[DELIVERY] Dispatched: %s %s (op=%s)",
            event.type,
            event.path,
            record.operation_id,
        )

    def _handle_dispatch_failure(
        self, session: "Session", record: Any, event: FileEvent, exc: Exception
    ) -> None:
        """Handle a failed dispatch: track retries or route to DLQ."""
        op_id = record.operation_id
        self._retry_counts[op_id] = self._retry_counts.get(op_id, 0) + 1
        retries = self._retry_counts[op_id]

        if retries >= self._max_retries:
            # Route to DLQ
            try:
                from nexus.services.event_subsystem.log.dead_letter import DeadLetterHandler

                handler = DeadLetterHandler()
                handler.route_to_dlq(
                    session,
                    operation_id=op_id,
                    exporter_name="internal",
                    error=exc,
                    event=event,
                    retry_count=retries,
                )
                self._total_dlq += 1
                self._retry_counts.pop(op_id, None)
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

    async def _dispatch_to_exporters(
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
            failures = await registry.dispatch_batch(events)
        except Exception:
            logger.exception("ExporterRegistry batch dispatch failed")
            return

        # Route per-exporter failures to DLQ
        if failures:
            from nexus.services.event_subsystem.log.dead_letter import DeadLetterHandler

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
