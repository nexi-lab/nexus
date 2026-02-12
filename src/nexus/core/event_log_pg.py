"""PGEventLog — PostgreSQL-backed EventLogProtocol fallback.

Wraps the existing ``operation_log`` table with the EventLogProtocol
interface.  Provides correctness and durability via PostgreSQL, but at
higher latency (~500μs–2ms per write) than the Rust WAL.

This is the automatic fallback when the ``_nexus_wal`` extension is not
installed.

Tracked by: #1397
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from nexus.core.event_bus import FileEvent
    from nexus.core.protocols.event_log import EventLogConfig

logger = logging.getLogger(__name__)


class PGEventLog:
    """EventLogProtocol implementation backed by PostgreSQL (operation_log).

    Requires a SQLAlchemy sync session factory.  All writes go through the
    existing ``OperationLogModel`` table.  Sequence numbers are the
    auto-generated ``ROWID`` / primary-key order.

    Note: This is a correctness-first fallback — see Decision #16 in the
    plan.  Performance is acceptable (~500μs–2ms) but not competitive with
    the Rust WAL's sub-5μs writes.
    """

    def __init__(self, config: EventLogConfig, session_factory: Any) -> None:  # noqa: ARG002
        self._session_factory = session_factory
        self._closed = False
        self._seq = 0  # monotonic counter for this process
        self._seq_lock = threading.Lock()
        logger.info("PGEventLog initialized (fallback mode)")

    # -- EventLogProtocol ---------------------------------------------------

    async def append(self, event: FileEvent) -> int:
        from nexus.storage.models.operation_log import OperationLogModel

        with self._seq_lock:
            self._seq += 1
            seq = self._seq

        record = OperationLogModel(
            operation_id=str(uuid.uuid4()),
            operation_type=_event_type_to_op(event.type),
            zone_id=event.zone_id or "default",
            agent_id=event.agent_id,
            path=event.path,
            new_path=event.old_path,
            status="success",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )

        with self._session_factory() as session:
            session.add(record)
            session.commit()

        return seq

    async def append_batch(self, events: list[FileEvent]) -> list[int]:
        from nexus.storage.models.operation_log import OperationLogModel

        seqs: list[int] = []
        records: list[OperationLogModel] = []

        with self._seq_lock:
            for event in events:
                self._seq += 1
                seqs.append(self._seq)
                records.append(
                    OperationLogModel(
                        operation_id=str(uuid.uuid4()),
                        operation_type=_event_type_to_op(event.type),
                        zone_id=event.zone_id or "default",
                        agent_id=event.agent_id,
                        path=event.path,
                        new_path=event.old_path,
                        status="success",
                        created_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )

        with self._session_factory() as session:
            session.add_all(records)
            session.commit()

        return seqs

    async def read_from(
        self,
        seq: int,  # noqa: ARG002 — protocol API; PG uses created_at ordering
        limit: int = 1000,
        *,
        zone_id: str | None = None,
    ) -> list[FileEvent]:
        from sqlalchemy import select

        from nexus.core.event_bus import FileEvent, FileEventType
        from nexus.storage.models.operation_log import OperationLogModel

        with self._session_factory() as session:
            stmt = select(OperationLogModel).order_by(OperationLogModel.created_at).limit(limit)
            if zone_id is not None:
                stmt = stmt.where(OperationLogModel.zone_id == zone_id)

            rows = session.execute(stmt).scalars().all()

        op_to_event = {
            "write": FileEventType.FILE_WRITE,
            "delete": FileEventType.FILE_DELETE,
            "rename": FileEventType.FILE_RENAME,
            "mkdir": FileEventType.DIR_CREATE,
            "rmdir": FileEventType.DIR_DELETE,
        }

        return [
            FileEvent(
                type=op_to_event.get(row.operation_type, row.operation_type),
                path=row.path,
                zone_id=row.zone_id,
                timestamp=row.created_at.isoformat() if row.created_at else "",
                old_path=row.new_path,
                agent_id=row.agent_id,
            )
            for row in rows
        ]

    async def truncate(self, before_seq: int) -> int:  # noqa: ARG002
        # PG fallback doesn't use WAL sequence numbers — truncation is a no-op.
        # Production cleanup uses scheduled SQL DELETE on old operation_log rows.
        logger.debug("PGEventLog.truncate is a no-op (use scheduled SQL cleanup)")
        return 0

    async def sync(self) -> None:
        # PostgreSQL handles durability via its own WAL — nothing to do.
        pass

    async def close(self) -> None:
        self._closed = True
        logger.info("PGEventLog closed")

    def current_sequence(self) -> int:
        return self._seq

    async def health_check(self) -> bool:
        if self._closed:
            return False
        try:
            with self._session_factory() as session:
                session.execute(__import__("sqlalchemy").text("SELECT 1"))
            return True
        except Exception as e:
            logger.warning("PGEventLog health check failed: %s", e)
            return False

    # -- Context manager ----------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_type_to_op(event_type: Any) -> str:
    """Convert FileEventType to operation_log operation_type string."""
    mapping = {
        "file_write": "write",
        "file_delete": "delete",
        "file_rename": "rename",
        "dir_create": "mkdir",
        "dir_delete": "rmdir",
        "metadata_change": "chmod",
    }
    type_str = event_type.value if hasattr(event_type, "value") else str(event_type)
    return mapping.get(type_str, type_str)
