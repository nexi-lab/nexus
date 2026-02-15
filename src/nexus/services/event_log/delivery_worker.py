"""EventDeliveryWorker — transactional outbox delivery (Issue #1241).

Polls ``operation_log`` rows where ``delivered = FALSE``, builds
``FileEvent`` objects from the existing columns, dispatches them to
downstream systems (EventBus, webhooks, hooks), and marks them
``delivered = TRUE`` in a single transaction.

Key guarantees:
- **At-least-once delivery**: events are only marked delivered after
  successful dispatch.  On crash, undelivered rows are retried.
- **Concurrent safety**: ``SELECT … FOR UPDATE SKIP LOCKED`` prevents
  two workers from processing the same batch.
- **Backpressure**: exponential backoff on consecutive empty polls
  (200ms → 400ms → 800ms → cap) to reduce idle DB pressure.

Tracked by: Issue #1241
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from nexus.core.event_bus import FileEvent, FileEventType

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---- Mapping: operation_type → FileEventType --------------------------------

_OP_TO_EVENT_TYPE: dict[str, FileEventType] = {
    "write": FileEventType.FILE_WRITE,
    "delete": FileEventType.FILE_DELETE,
    "rename": FileEventType.FILE_RENAME,
    "mkdir": FileEventType.DIR_CREATE,
    "rmdir": FileEventType.DIR_DELETE,
    "chmod": FileEventType.METADATA_CHANGE,
    "chown": FileEventType.METADATA_CHANGE,
    "chgrp": FileEventType.METADATA_CHANGE,
    "setfacl": FileEventType.METADATA_CHANGE,
}


class EventDeliveryWorker:
    """Background worker polling undelivered events from operation_log.

    Uses ``SELECT FOR UPDATE SKIP LOCKED`` for concurrent worker safety.
    Dispatches to EventBus (Redis Pub/Sub), webhooks, and hooks.
    Marks events ``delivered = TRUE`` after successful dispatch.
    Retries with exponential backoff on failure.
    """

    def __init__(
        self,
        session_factory: Callable[..., Any],
        event_bus: Any | None = None,
        *,
        poll_interval_ms: int = 200,
        batch_size: int = 50,
        max_retries: int = 3,
        max_backoff_ms: int = 5000,
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._poll_interval_s = poll_interval_ms / 1000.0
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._max_backoff_s = max_backoff_ms / 1000.0

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._consecutive_empty = 0

        # Metrics
        self._total_dispatched = 0
        self._total_failed = 0

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
            "EventDeliveryWorker started (poll=%.0fms, batch=%d)",
            self._poll_interval_s * 1000,
            self._batch_size,
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
            "EventDeliveryWorker stopped (dispatched=%d, failed=%d)",
            self._total_dispatched,
            self._total_failed,
        )

    @property
    def metrics(self) -> dict[str, int]:
        return {
            "total_dispatched": self._total_dispatched,
            "total_failed": self._total_failed,
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

    # ---- Core: poll → dispatch → mark ----------------------------------------

    def _poll_and_dispatch(self) -> int:
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

            # PostgreSQL: row-level locking for concurrent workers
            dialect_name = session.bind.dialect.name if session.bind else ""
            if dialect_name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)

            rows = list(session.execute(stmt).scalars())

            if not rows:
                return 0

            # Dispatch each event; track successes
            for record in rows:
                try:
                    self._dispatch_event(record)
                    dispatched_ids.append(record.operation_id)
                    self._total_dispatched += 1
                except Exception:
                    logger.warning(
                        "Failed to dispatch event %s (%s %s)",
                        record.operation_id,
                        record.operation_type,
                        record.path,
                        exc_info=True,
                    )
                    self._total_failed += 1

            # Mark successfully dispatched rows as delivered
            if dispatched_ids:
                self._mark_delivered(session, dispatched_ids)
                session.commit()

        return len(dispatched_ids)

    def _dispatch_event(self, record: Any) -> None:
        """Build FileEvent from record and dispatch to downstream systems."""
        event = self._build_file_event(record)

        # 1. Publish to global EventBus (Redis Pub/Sub)
        bus = self._event_bus
        if bus is None:
            try:
                from nexus.core.event_bus import get_global_event_bus

                bus = get_global_event_bus()
            except ImportError:
                pass

        if bus is not None:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(bus.publish(event), loop)
            else:
                # No running loop — create a temporary one
                asyncio.run(bus.publish(event))

        # 2. Broadcast to webhook subscriptions
        try:
            from nexus.server.subscriptions import get_subscription_manager

            sub_manager = get_subscription_manager()
            if sub_manager is not None:
                import asyncio as _asyncio

                event_type_str = str(event.type)

                async def _broadcast() -> None:
                    await sub_manager.broadcast(
                        event_type=event_type_str,
                        data={
                            "file_path": event.path,
                            "old_path": event.old_path,
                            "size": event.size,
                            "timestamp": event.timestamp,
                        },
                        zone_id=event.zone_id or "default",
                    )

                try:
                    loop = _asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None and loop.is_running():
                    _asyncio.run_coroutine_threadsafe(_broadcast(), loop)
                else:
                    _asyncio.run(_broadcast())
        except ImportError:
            pass  # Subscription manager not available

        logger.debug(
            "[DELIVERY] Dispatched: %s %s (op=%s)",
            event.type,
            event.path,
            record.operation_id,
        )

    def _build_file_event(self, record: Any) -> FileEvent:
        """Build a FileEvent from an OperationLogModel record."""
        event_type = _OP_TO_EVENT_TYPE.get(record.operation_type, record.operation_type)

        return FileEvent(
            type=event_type,
            path=record.path,
            zone_id=record.zone_id or "default",
            timestamp=record.created_at.isoformat() if record.created_at else "",
            old_path=record.new_path,  # new_path stores old_path for renames
            agent_id=record.agent_id,
        )

    def _mark_delivered(self, session: Session, operation_ids: list[str]) -> None:
        """Mark operation_log rows as delivered."""
        from sqlalchemy import update

        from nexus.storage.models.operation_log import OperationLogModel

        session.execute(
            update(OperationLogModel)
            .where(OperationLogModel.operation_id.in_(operation_ids))
            .values(delivered=True)
        )
