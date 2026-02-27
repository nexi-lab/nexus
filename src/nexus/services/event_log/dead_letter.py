"""DeadLetterHandler — routes failed exports to the dead letter queue.

Provides DLQ write, query, and replay operations for events that
failed export to external systems after exhausting retries.

Issue #1138: Event Stream Export.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.core.file_events import FileEvent

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.services.event_log.exporter_registry import ExporterRegistry

logger = logging.getLogger(__name__)

# ---- Error classification ---------------------------------------------------

_TRANSIENT_ERRORS = (ConnectionError, TimeoutError, OSError)


def classify_error(error: Exception) -> str:
    """Classify an error as transient or permanent."""
    if isinstance(error, _TRANSIENT_ERRORS):
        return "transient"
    return "permanent"


class DeadLetterHandler:
    """Manages the dead letter queue for failed event exports."""

    def route_to_dlq(
        self,
        session: Session,
        *,
        operation_id: str,
        exporter_name: str,
        error: Exception,
        event: FileEvent,
        retry_count: int = 0,
    ) -> None:
        """Insert a failed event into the dead letter queue."""
        from nexus.storage.models.dead_letter import DeadLetterModel

        entry = DeadLetterModel(
            operation_id=operation_id,
            exporter_name=exporter_name,
            event_payload=json.dumps(event.to_dict()),
            failure_type=classify_error(error),
            error_message=str(error)[:2000],
            retry_count=retry_count,
        )
        session.add(entry)
        logger.warning(
            "DLQ: routed op=%s exporter=%s error=%s",
            operation_id,
            exporter_name,
            type(error).__name__,
        )

    def list_unresolved(
        self,
        session: Session,
        *,
        exporter_name: str | None = None,
        limit: int = 100,
    ) -> list:
        """List unresolved DLQ entries."""
        from sqlalchemy import select

        from nexus.storage.models.dead_letter import DeadLetterModel

        stmt = (
            select(DeadLetterModel)
            .where(DeadLetterModel.resolved_at.is_(None))
            .order_by(DeadLetterModel.created_at)
            .limit(limit)
        )
        if exporter_name is not None:
            stmt = stmt.where(DeadLetterModel.exporter_name == exporter_name)
        return list(session.execute(stmt).scalars())

    async def replay_dlq(
        self,
        session: Session,
        exporter_registry: ExporterRegistry,
        *,
        limit: int = 100,
    ) -> int:
        """Replay unresolved DLQ entries through the exporter registry.

        Returns the number of successfully replayed entries.
        """
        entries = self.list_unresolved(session, limit=limit)
        if not entries:
            return 0

        events = []
        entry_map: dict[str, list] = {}  # event_id → DLQ entries
        for entry in entries:
            event = FileEvent.from_dict(json.loads(entry.event_payload))
            events.append(event)
            entry_map.setdefault(event.event_id, []).append(entry)

        failures = await exporter_registry.dispatch_batch(events)
        all_failed_ids: set[str] = set()
        for failed_ids in failures.values():
            all_failed_ids.update(failed_ids)

        resolved_count = 0
        now = datetime.now(UTC)
        for event in events:
            if event.event_id not in all_failed_ids:
                for dlq_entry in entry_map.get(event.event_id, []):
                    dlq_entry.resolved_at = now
                    resolved_count += 1

        if resolved_count:
            session.flush()
        return resolved_count
