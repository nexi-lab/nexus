"""Recall Store - Secondary memory tier (sequential access).

Wraps memory_api for temporal/sequential access patterns.
Optimized for recency queries: "What happened recently?", "Show last 50 messages"

Thread-safe: Each operation creates its own session from the session factory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from nexus.core.memory_paging.namespace_util import strip_tier_prefix

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)


class RecallStore:
    """Manages recall storage (secondary memory tier).

    Provides temporal/sequential access to recent memory history.
    Wraps existing memory_api with recall-specific query patterns.

    Thread-safe: Uses session_factory to create per-operation sessions.

    Example:
        >>> recall = RecallStore(session_factory, zone_id="acme")
        >>> recall.append(memory)
        >>> recent = recall.get_recent(limit=50)
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        zone_id: str = "default",
        namespace: str = "recall",
    ):
        """Initialize recall store.

        Args:
            session_factory: Callable that returns a new SQLAlchemy session
            zone_id: Zone ID for multi-tenancy
            namespace: Namespace for recall memories (default: "recall")
        """
        self._session_factory = session_factory
        self.zone_id = zone_id
        self.namespace = namespace

    def append(self, memory: MemoryModel) -> None:
        """Append memory to recall storage.

        Merges the (possibly detached) memory into a fresh session, updates
        its namespace to the recall tier, and commits.

        Args:
            memory: Memory to store in recall
        """
        session = self._session_factory()
        try:
            # Merge first to get a session-bound copy (handles detached objects)
            merged = session.merge(memory)
            merged.namespace = f"{self.namespace}/{strip_tier_prefix(merged.namespace)}"

            session.commit()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Appended memory {merged.memory_id} to recall store")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def append_batch(self, memories: list[MemoryModel]) -> None:
        """Append multiple memories to recall in a single transaction.

        Merges all memories, updates namespaces, and commits once.
        Reduces N DB round trips to 1.

        Args:
            memories: Memories to store in recall
        """
        if not memories:
            return
        session = self._session_factory()
        try:
            for memory in memories:
                merged = session.merge(memory)
                merged.namespace = f"{self.namespace}/{strip_tier_prefix(merged.namespace)}"
            session.commit()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Batch appended {len(memories)} memories to recall store")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_recent(self, limit: int = 100) -> list[MemoryModel]:
        """Get most recent memories from recall.

        Args:
            limit: Maximum memories to return

        Returns:
            List of memories, most recent first
        """
        session = self._session_factory()
        try:
            from nexus.services.memory.memory_router import MemoryViewRouter

            router = MemoryViewRouter(session)
            memories = router.query_memories(
                zone_id=self.zone_id,
                namespace_prefix=self.namespace,
                limit=limit,
            )
            return memories  # Already ordered by created_at DESC
        finally:
            session.close()

    def query_temporal(
        self,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int | None = None,
    ) -> list[MemoryModel]:
        """Query recall by time range.

        Args:
            after: Start of time range (inclusive)
            before: End of time range (inclusive)
            limit: Maximum results

        Returns:
            List of memories in time range, most recent first
        """
        session = self._session_factory()
        try:
            from nexus.services.memory.memory_router import MemoryViewRouter

            router = MemoryViewRouter(session)
            return router.query_memories(
                zone_id=self.zone_id,
                namespace_prefix=self.namespace,
                after=after,
                before=before,
                limit=limit,
            )
        finally:
            session.close()

    def count(self) -> int:
        """Get count of memories in recall store."""
        session = self._session_factory()
        try:
            from sqlalchemy import func, select

            from nexus.storage.models import MemoryModel

            stmt = (
                select(func.count())
                .select_from(MemoryModel)
                .where(
                    MemoryModel.zone_id == self.zone_id,
                    MemoryModel.namespace.like(f"{self.namespace}%"),
                    MemoryModel.state == "active",
                )
            )
            return session.execute(stmt).scalar_one()
        finally:
            session.close()

    def remove(self, memory_id: str) -> bool:
        """Remove memory from recall store.

        Args:
            memory_id: Memory ID to remove

        Returns:
            True if removed, False if not found
        """
        session = self._session_factory()
        try:
            from nexus.services.memory.memory_router import MemoryViewRouter

            router = MemoryViewRouter(session)
            result = router.delete_memory(memory_id)
            return result
        finally:
            session.close()

    def get_oldest_timestamp(self) -> datetime | None:
        """Get timestamp of oldest active memory in recall."""
        session = self._session_factory()
        try:
            from sqlalchemy import select

            from nexus.storage.models import MemoryModel

            stmt = (
                select(MemoryModel.created_at)
                .where(
                    MemoryModel.zone_id == self.zone_id,
                    MemoryModel.namespace.like(f"{self.namespace}%"),
                    MemoryModel.state == "active",
                )
                .order_by(MemoryModel.created_at.asc())
                .limit(1)
            )
            return session.execute(stmt).scalar_one_or_none()
        finally:
            session.close()
