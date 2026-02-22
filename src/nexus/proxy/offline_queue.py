"""WAL-backed offline queue for proxy operations.

Uses SQLAlchemy async ORM with aiosqlite for crash-safe persistence.
"""

import hashlib
import json
import os
import time
from typing import Any, cast

from sqlalchemy import Table, delete, event, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.proxy.queue_protocol import QueuedOperation
from nexus.storage.models._base import Base
from nexus.storage.models.sync import PendingOperationModel as PO


class OfflineQueue:
    """Persistent offline operation queue backed by SQLAlchemy async ORM.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    max_retry_count:
        Default max retries before an operation is dead-lettered.
    """

    def __init__(self, db_path: str, max_retry_count: int = 10) -> None:
        self._db_path = os.path.expanduser(db_path)
        self._max_retry_count = max_retry_count
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def initialize(self) -> None:
        """Open the database, enable WAL mode, and create the schema."""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)

        url = f"sqlite+aiosqlite:///{self._db_path}"
        self._engine = create_async_engine(url)

        # Enable WAL mode via connection event listener (driver-agnostic approach)
        @event.listens_for(self._engine.sync_engine, "connect")
        def _set_sqlite_wal(dbapi_conn: Any, _connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[cast(Table, PO.__table__)])

        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    def _get_session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        return self._session_factory

    @staticmethod
    def _generate_idempotency_key(method: str, kwargs: dict[str, Any] | None) -> str:
        """Derive a deterministic idempotency key from method + kwargs."""
        canonical = json.dumps({"m": method, "k": kwargs or {}}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    async def enqueue(
        self,
        method: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        payload_ref: str | None = None,
        vector_clock: str | None = None,
        priority: int = 0,
    ) -> int:
        """Add an operation to the queue.  Returns the row id."""
        factory = self._get_session_factory()
        idem_key = self._generate_idempotency_key(method, kwargs)
        async with factory() as session:
            op = PO(
                method=method,
                args_json=json.dumps(args),
                kwargs_json=json.dumps(kwargs or {}),
                payload_ref=payload_ref,
                created_at=time.time(),
                max_retries=self._max_retry_count,
                idempotency_key=idem_key,
                vector_clock=vector_clock,
                priority=priority,
            )
            session.add(op)
            await session.commit()
            return op.id

    def _row_to_op(self, r: PO) -> QueuedOperation:
        """Convert a PendingOperationModel row to a QueuedOperation."""
        return QueuedOperation(
            id=r.id,
            method=r.method,
            args_json=r.args_json,
            kwargs_json=r.kwargs_json,
            payload_ref=r.payload_ref,
            retry_count=r.retry_count,
            created_at=r.created_at,
            idempotency_key=r.idempotency_key,
            vector_clock=r.vector_clock,
            priority=r.priority,
        )

    async def dequeue_batch(self, limit: int = 50) -> list[QueuedOperation]:
        """Fetch up to *limit* pending operations (FIFO order)."""
        factory = self._get_session_factory()
        async with factory() as session:
            stmt = select(PO).where(PO.status == "pending").order_by(PO.id).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._row_to_op(r) for r in rows]

    async def dequeue_by_priority(self, limit: int = 50) -> list[QueuedOperation]:
        """Fetch pending operations ordered by priority (desc), then id (asc)."""
        factory = self._get_session_factory()
        async with factory() as session:
            stmt = (
                select(PO)
                .where(PO.status == "pending")
                .order_by(PO.priority.desc(), PO.id)
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._row_to_op(r) for r in rows]

    async def has_idempotency_key(self, key: str) -> bool:
        """Check if an operation with this idempotency key was already completed."""
        factory = self._get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(PO)
                .where(PO.idempotency_key == key, PO.status == "done")
            )
            return result.scalar_one() > 0

    async def mark_done(self, op_id: int) -> None:
        """Mark an operation as successfully replayed."""
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(update(PO).where(PO.id == op_id).values(status="done"))
            await session.commit()

    async def mark_failed(self, op_id: int) -> None:
        """Increment retry count; dead-letter if max retries exceeded."""
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(
                update(PO).where(PO.id == op_id).values(retry_count=PO.retry_count + 1)
            )
            await session.execute(
                update(PO)
                .where(PO.id == op_id, PO.retry_count >= PO.max_retries)
                .values(status="dead_letter")
            )
            await session.commit()

    async def mark_dead_letter(self, op_id: int) -> None:
        """Explicitly move an operation to the dead-letter status."""
        factory = self._get_session_factory()
        async with factory() as session:
            await session.execute(update(PO).where(PO.id == op_id).values(status="dead_letter"))
            await session.commit()

    async def pending_count(self) -> int:
        """Return the number of pending operations."""
        factory = self._get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(func.count()).select_from(PO).where(PO.status == "pending")
            )
            return result.scalar_one()

    async def cleanup_completed(self, older_than_seconds: float = 3600) -> int:
        """Delete completed operations older than *older_than_seconds*."""
        factory = self._get_session_factory()
        cutoff = time.time() - older_than_seconds
        async with factory() as session:
            # Count matching rows first, then delete
            count_result = await session.execute(
                select(func.count())
                .select_from(PO)
                .where(PO.status == "done", PO.created_at < cutoff)
            )
            count: int = count_result.scalar_one()
            if count > 0:
                await session.execute(delete(PO).where(PO.status == "done", PO.created_at < cutoff))
                await session.commit()
            return count

    async def close(self) -> None:
        """Close the database connection."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self._session_factory = None
