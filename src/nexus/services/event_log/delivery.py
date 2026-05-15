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
- **Signal-driven wakeup**: the write observer sets an ``asyncio.Event``
  after each flush so the worker wakes immediately instead of polling
  on a fixed timer (Issue #3193).
- **DLQ routing**: events failing after max_retries are sent to the
  dead letter queue (Issue #1138).

Tracked by: Issue #1241, #1138, #3193
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.operation_types import OperationType
from nexus.services.event_bus.types import FileEvent, FileEventType

if TYPE_CHECKING:
    from nexus.services.event_log.exporter_registry import ExporterRegistry
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


@dataclass(frozen=True, slots=True)
class _PendingDelivery:
    """Detached operation_log row data safe to use outside a DB session."""

    event: FileEvent
    operation_id: str
    retry_count: int


class EventDeliveryWorker:
    """Background worker polling undelivered events from operation_log.

    Uses ``SELECT FOR UPDATE SKIP LOCKED`` for concurrent worker safety.
    Dispatches to EventBus (Redis Pub/Sub), webhooks, hooks, and external
    exporters (Kafka, NATS, Pub/Sub via ExporterRegistry).
    Marks events ``delivered = TRUE`` after successful dispatch.
    Routes failures to DLQ after max_retries.

    Issue #3193: converted from threading.Thread to asyncio.Task with
    drain-then-wait pattern driven by an ``asyncio.Event`` signal.
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        event_bus: Any | None = None,
        exporter_registry: "ExporterRegistry | None" = None,
        subscription_manager_getter: Callable[[], Any] | None = None,
        *,
        event_signal: asyncio.Event | None = None,
        fallback_poll_interval_s: float = 5.0,
        batch_size: int = 50,
        max_retries: int = 3,
        max_backoff_ms: int = 5000,
        use_row_locking: bool = False,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._event_bus = event_bus
        self._exporter_registry = exporter_registry
        self._subscription_manager_getter = subscription_manager_getter
        self._event_signal = event_signal
        self._fallback_poll_interval_s = fallback_poll_interval_s
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._max_backoff_s = max_backoff_ms / 1000.0
        self._use_row_locking = use_row_locking

        self._consumer_task: asyncio.Task[None] | None = None
        self._stopped = False

        # Metrics
        self._total_dispatched = 0
        self._total_failed = 0
        self._total_dlq = 0

    # ---- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start background consumer task (BackgroundService protocol)."""
        if self._consumer_task is not None and not self._consumer_task.done():
            logger.warning("EventDeliveryWorker already running")
            return

        self._stopped = False
        self._consumer_task = asyncio.create_task(self._consume())
        logger.info(
            "EventDeliveryWorker started (fallback_poll=%.1fs, batch=%d, exporters=%s)",
            self._fallback_poll_interval_s,
            self._batch_size,
            self._exporter_registry.exporter_names if self._exporter_registry else "none",
        )

    async def stop(self, timeout: float = 5.0) -> None:  # noqa: ARG002
        """Graceful shutdown: cancel task and await completion (BackgroundService protocol)."""
        self._stopped = True
        if self._consumer_task is not None:
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

    # ---- Main loop (drain-then-wait, Issue #3193) ----------------------------

    async def _consume(self) -> None:
        """Drain-then-wait consumer loop.

        1. Clear the signal (so we don't miss edges).
        2. Poll and dispatch all undelivered rows.
        3. If rows were found, loop immediately (drain).
        4. If no rows, wait on the signal (or fallback sleep).
        """
        signal = self._event_signal
        while not self._stopped:
            try:
                if signal is not None:
                    signal.clear()
                count = await self._poll_and_dispatch()
                if count > 0:
                    continue
                if signal is not None:
                    await signal.wait()
                else:
                    await asyncio.sleep(self._fallback_poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EventDeliveryWorker consume error")
                await asyncio.sleep(1.0)

    # ---- Core: poll -> dispatch -> mark --------------------------------------

    async def _poll_and_dispatch(self) -> int:
        """Poll undelivered rows, dispatch events, mark delivered.

        Returns:
            Number of events successfully dispatched.
        """
        pending = await asyncio.to_thread(self._load_pending_deliveries)
        if not pending:
            return 0
        dispatched_ids: list[str] = []

        # 1. Dispatch events to EventBus + webhooks, ordered per zone (#2755).
        #    Group by zone_id and dispatch sequentially within each zone
        #    (parallel across zones is safe — no cross-zone causal deps).
        def zone_key(item: _PendingDelivery) -> str:
            return item.event.zone_id or ROOT_ZONE_ID

        sorted_items = sorted(pending, key=zone_key)
        from itertools import groupby

        for _zone_id, zone_group in groupby(sorted_items, key=zone_key):
            for item in zone_group:
                try:
                    await self._dispatch_event_internal(item.event, item)
                    dispatched_ids.append(item.operation_id)
                    self._total_dispatched += 1
                except Exception as exc:
                    await asyncio.to_thread(self._handle_dispatch_failure, item, exc)

        # 2. Dispatch batch to external exporters (parallel)
        if self._exporter_registry and self._exporter_registry.exporter_names:
            await self._dispatch_to_exporters(pending)

        # Mark successfully dispatched rows as delivered
        if dispatched_ids:
            await asyncio.to_thread(self._mark_delivered, dispatched_ids)

        return len(dispatched_ids)

    def _load_pending_deliveries(self) -> list[_PendingDelivery]:
        """Load pending operation_log rows without blocking the event loop."""
        from sqlalchemy import select

        from nexus.storage.models.operation_log import OperationLogModel

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
            return [
                _PendingDelivery(
                    event=self._build_file_event(record),
                    operation_id=record.operation_id,
                    retry_count=record.retry_count,
                )
                for record in rows
            ]

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
                event_type_str = str(event.type)
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

        logger.debug(
            "[DELIVERY] Dispatched: %s %s (op=%s)",
            event.type,
            event.path,
            record.operation_id,
        )

    def _handle_dispatch_failure(self, item: _PendingDelivery, exc: Exception) -> None:
        """Handle a failed dispatch: increment persistent retry_count or route to DLQ.

        Uses ``record.retry_count`` (persisted in DB) instead of an in-memory
        dict so retry state survives worker restarts (Issue #2751).
        """
        from nexus.storage.models.operation_log import OperationLogModel

        op_id = item.operation_id
        with self._session_factory() as session:
            record = session.get(OperationLogModel, op_id)
            if record is None:
                return

            record.retry_count = record.retry_count + 1
            retries = record.retry_count

            if retries >= self._max_retries:
                # Route to DLQ and mark delivered to stop re-polling
                try:
                    from nexus.services.event_log.dead_letter import DeadLetterHandler

                    handler = DeadLetterHandler()
                    handler.route_to_dlq(
                        session,
                        operation_id=op_id,
                        exporter_name="internal",
                        error=exc,
                        event=item.event,
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
            session.commit()
        self._total_failed += 1

    async def _dispatch_to_exporters(
        self,
        pending: list[_PendingDelivery],
    ) -> None:
        """Dispatch event batch to external exporters via ExporterRegistry."""
        registry = self._exporter_registry
        if not registry:
            return

        events = [item.event for item in pending]
        try:
            failures = await registry.dispatch_batch(events)
        except Exception:
            logger.exception("ExporterRegistry batch dispatch failed")
            return

        # Route per-exporter failures to DLQ
        if failures:
            await asyncio.to_thread(self._route_exporter_failures, failures, pending)

    def _route_exporter_failures(
        self,
        failures: dict[str, list[str]],
        pending: list[_PendingDelivery],
    ) -> None:
        """Persist exporter DLQ entries without blocking the event loop."""
        from nexus.services.event_log.dead_letter import DeadLetterHandler

        handler = DeadLetterHandler()
        id_map = {item.event.event_id: item for item in pending}

        with self._session_factory() as session:
            for exporter_name, failed_ids in failures.items():
                for event_id in failed_ids:
                    item = id_map.get(event_id)
                    if item is None:
                        continue
                    handler.route_to_dlq(
                        session,
                        operation_id=item.operation_id,
                        exporter_name=exporter_name,
                        error=ConnectionError(f"Export to {exporter_name} failed"),
                        event=item.event,
                    )
                    self._total_dlq += 1
            session.commit()

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

    def _mark_delivered(self, operation_ids: list[str]) -> None:
        """Mark operation_log rows as delivered."""
        from sqlalchemy import update

        from nexus.storage.models.operation_log import OperationLogModel

        with self._session_factory() as session:
            session.execute(
                update(OperationLogModel)
                .where(OperationLogModel.operation_id.in_(operation_ids))
                .values(delivered=True)
            )
            session.commit()
