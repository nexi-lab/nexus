"""Recall Store - Secondary memory tier (sequential access).

Wraps memory_api for temporal/sequential access patterns.
Optimized for recency queries: "What happened recently?", "Show last 50 messages"
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)


class RecallStore:
    """Manages recall storage (secondary memory tier).

    Provides temporal/sequential access to recent memory history.
    Wraps existing memory_api with recall-specific query patterns.

    Example:
        >>> recall = RecallStore(session, zone_id="acme")
        >>> recall.append(memory)
        >>> recent = recall.get_recent(limit=50)
    """

    def __init__(
        self,
        session: Session,
        zone_id: str = "default",
        namespace: str = "recall",
    ):
        """Initialize recall store.

        Args:
            session: SQLAlchemy session
            zone_id: Zone ID for multi-tenancy
            namespace: Namespace for recall memories (default: "recall")
        """
        self.session = session
        self.zone_id = zone_id
        self.namespace = namespace

        # Import here to avoid circular dependency
        from nexus.core.memory_router import MemoryViewRouter

        self.router = MemoryViewRouter(session)

    def append(self, memory: MemoryModel) -> None:
        """Append memory to recall storage.

        Updates namespace to mark as recall tier.

        Args:
            memory: Memory to store in recall
        """
        # Update namespace to indicate recall tier
        if not memory.namespace or not memory.namespace.startswith(self.namespace):
            memory.namespace = f"{self.namespace}/{memory.namespace or 'default'}"

        # Ensure memory is in session
        if memory not in self.session:
            self.session.add(memory)

        self.session.commit()
        logger.debug(f"Appended memory {memory.memory_id} to recall store")

    def get_recent(self, limit: int = 100) -> list[MemoryModel]:
        """Get most recent memories from recall.

        Args:
            limit: Maximum memories to return

        Returns:
            List of memories, most recent first
        """
        memories = self.router.query_memories(
            zone_id=self.zone_id,
            namespace_prefix=self.namespace,
            limit=limit,
        )
        return memories  # Already ordered by created_at DESC

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
        return self.router.query_memories(
            zone_id=self.zone_id,
            namespace_prefix=self.namespace,
            after=after,
            before=before,
            limit=limit,
        )

    def count(self) -> int:
        """Get count of memories in recall store."""
        memories = self.router.query_memories(
            zone_id=self.zone_id,
            namespace_prefix=self.namespace,
        )
        return len(memories)

    def remove(self, memory_id: str) -> bool:
        """Remove memory from recall store.

        Args:
            memory_id: Memory ID to remove

        Returns:
            True if removed, False if not found
        """
        return self.router.delete_memory(memory_id)

    def get_oldest_timestamp(self) -> datetime | None:
        """Get timestamp of oldest memory in recall."""
        # Query with limit=1, no order specified will get oldest
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        stmt = (
            select(MemoryModel.created_at)
            .where(MemoryModel.zone_id == self.zone_id)
            .where(MemoryModel.namespace.like(f"{self.namespace}%"))
            .order_by(MemoryModel.created_at.asc())
            .limit(1)
        )
        result = self.session.execute(stmt).scalar_one_or_none()
        return result
