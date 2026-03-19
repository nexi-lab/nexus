"""EventReplayService — replay and stream historical events (Issue #1139).

Provides cursor-based pagination over operation_log using sequence_number,
plus an async generator for SSE streaming with poll-based tail.

Shares filter logic with OperationLogger._apply_filters() where possible.
"""

import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventRecord:
    """Lightweight event record for replay responses."""

    event_id: str
    type: str
    path: str
    new_path: str | None
    zone_id: str
    agent_id: str | None
    status: str
    delivered: bool
    timestamp: str
    sequence_number: int | None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "type": self.type,
            "path": self.path,
            "zone_id": self.zone_id,
            "status": self.status,
            "delivered": self.delivered,
            "timestamp": self.timestamp,
        }
        if self.new_path is not None:
            result["new_path"] = self.new_path
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.sequence_number is not None:
            result["sequence_number"] = self.sequence_number
        return result


@dataclass(frozen=True)
class ReplayResult:
    """Result of a replay query with cursor pagination."""

    events: list[EventRecord]
    next_cursor: str | None
    has_more: bool


def _encode_cursor(seq: int) -> str:
    """Encode a sequence_number into an opaque cursor string."""
    return base64.urlsafe_b64encode(json.dumps({"s": seq}).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    """Decode a cursor string back to sequence_number.

    Raises:
        ValueError: If the cursor is malformed or cannot be decoded.
    """
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor))
        return int(data["s"])
    except Exception as exc:
        raise ValueError(f"Invalid replay cursor: {cursor!r}") from exc


def _record_from_row(row: Any) -> EventRecord:
    """Convert an OperationLogModel row to an EventRecord."""
    return EventRecord(
        event_id=row.operation_id,
        type=row.operation_type,
        path=row.path,
        new_path=row.new_path,
        zone_id=row.zone_id,
        agent_id=row.agent_id,
        status=row.status,
        delivered=row.delivered,
        timestamp=row.created_at.isoformat() if row.created_at else "",
        sequence_number=row.sequence_number,
    )


class EventReplayService:
    """Service for replaying and streaming historical events from operation_log."""

    def __init__(self, record_store: "RecordStoreABC") -> None:
        self._session_factory = record_store.session_factory

    def replay(
        self,
        *,
        zone_id: str | None = None,
        since_revision: int | None = None,
        since_timestamp: datetime | None = None,
        event_types: list[str] | None = None,
        path_pattern: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ReplayResult:
        """Query historical events with cursor-based pagination.

        Cursor is based on sequence_number for stable, gap-free ordering.
        Uses LIMIT N+1 trick for has_more detection.
        """
        from sqlalchemy import select

        from nexus.storage.models.operation_log import OperationLogModel

        with self._session_factory() as session:
            stmt = select(OperationLogModel).order_by(OperationLogModel.sequence_number.asc())

            # Apply cursor (sequence_number based)
            if cursor is not None:
                seq = _decode_cursor(cursor)
                stmt = stmt.where(OperationLogModel.sequence_number > seq)

            # Apply since_revision (sequence_number based)
            if since_revision is not None and cursor is None:
                stmt = stmt.where(OperationLogModel.sequence_number > since_revision)

            # Apply filters
            stmt = self._apply_filters(
                stmt,
                zone_id=zone_id,
                since_timestamp=since_timestamp,
                event_types=event_types,
                path_pattern=path_pattern,
                agent_id=agent_id,
            )

            # Fetch one extra for has_more detection
            stmt = stmt.limit(limit + 1)
            rows = list(session.execute(stmt).scalars())

            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            events = [_record_from_row(row) for row in rows]

            next_cursor = None
            if has_more and events and events[-1].sequence_number is not None:
                next_cursor = _encode_cursor(events[-1].sequence_number)

            return ReplayResult(
                events=events,
                next_cursor=next_cursor,
                has_more=has_more,
            )

    async def stream(
        self,
        *,
        zone_id: str | None = None,
        since_revision: int | None = None,
        since_timestamp: datetime | None = None,
        event_types: list[str] | None = None,
        path_pattern: str | None = None,
        agent_id: str | None = None,
        poll_interval: float = 1.0,
        idle_timeout: float = 300.0,
    ) -> AsyncIterator[EventRecord]:
        """Async generator that yields events, polling for new ones.

        Yields historical events first (if since_revision/since_timestamp given),
        then polls for new events until idle_timeout is reached.
        """
        import asyncio

        last_seq = since_revision
        idle_elapsed = 0.0

        while True:
            result = self.replay(
                zone_id=zone_id,
                since_revision=last_seq,
                since_timestamp=since_timestamp if last_seq is None else None,
                event_types=event_types,
                path_pattern=path_pattern,
                agent_id=agent_id,
                limit=100,
            )

            if result.events:
                idle_elapsed = 0.0
                for event in result.events:
                    yield event
                    if event.sequence_number is not None:
                        last_seq = event.sequence_number
            else:
                idle_elapsed += poll_interval
                if idle_elapsed >= idle_timeout:
                    return

            await asyncio.sleep(poll_interval)

    def list_v1(
        self,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        operation_type: str | None = None,
        path_prefix: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ReplayResult:
        """V1-compatible event query: DESC ordering, operation_id cursor.

        Preserves the same semantics as OperationLogger.list_operations_cursor()
        so the v1 API contract is unchanged. This centralises event querying
        in EventReplayService while keeping backward compatibility.
        """
        from sqlalchemy import desc, select

        from nexus.storage.models.operation_log import OperationLogModel

        with self._session_factory() as session:
            stmt = select(OperationLogModel).order_by(
                desc(OperationLogModel.created_at),
                desc(OperationLogModel.operation_id),
            )

            # Apply operation_id cursor (composite: created_at + operation_id)
            if cursor:
                cursor_op = session.execute(
                    select(OperationLogModel).where(OperationLogModel.operation_id == cursor)
                ).scalar_one_or_none()
                if cursor_op:
                    stmt = stmt.where(
                        (OperationLogModel.created_at < cursor_op.created_at)
                        | (
                            (OperationLogModel.created_at == cursor_op.created_at)
                            & (OperationLogModel.operation_id < cursor)
                        )
                    )

            # Apply filters
            stmt = self._apply_filters(
                stmt,
                zone_id=zone_id,
                agent_id=agent_id,
                since_timestamp=since,
                path_pattern=path_prefix,
            )
            if operation_type is not None:
                stmt = stmt.where(OperationLogModel.operation_type == operation_type)
            if until is not None:
                stmt = stmt.where(OperationLogModel.created_at <= until)

            # LIMIT N+1 for has_more detection
            stmt = stmt.limit(limit + 1)
            rows = list(session.execute(stmt).scalars())

            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            events = [_record_from_row(row) for row in rows]

            next_cursor = events[-1].event_id if has_more and events else None

            return ReplayResult(
                events=events,
                next_cursor=next_cursor,
                has_more=has_more,
            )

    @staticmethod
    def _apply_filters(
        stmt: Any,
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
        since_timestamp: datetime | None = None,
        event_types: list[str] | None = None,
        path_pattern: str | None = None,
    ) -> Any:
        """Apply shared WHERE clauses to the query."""
        from nexus.storage.models.operation_log import OperationLogModel

        if zone_id is not None:
            stmt = stmt.where(OperationLogModel.zone_id == zone_id)
        if agent_id is not None:
            stmt = stmt.where(OperationLogModel.agent_id == agent_id)
        if since_timestamp is not None:
            stmt = stmt.where(OperationLogModel.created_at >= since_timestamp)
        if event_types:
            stmt = stmt.where(OperationLogModel.operation_type.in_(event_types))
        if path_pattern:
            # Convert glob pattern to SQL LIKE
            escaped = path_pattern.replace("%", r"\%").replace("_", r"\_")
            like_pattern = escaped.replace("*", "%").replace("?", "_")
            stmt = stmt.where(OperationLogModel.path.like(like_pattern, escape="\\"))
        return stmt
