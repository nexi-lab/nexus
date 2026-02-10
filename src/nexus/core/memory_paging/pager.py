"""Memory Pager - Orchestrates 3-tier memory system.

Manages memory flow between main context, recall, and archival tiers.
Automatically pages memories based on capacity and age.

Thread-safe: ContextManager uses locks, stores use per-operation sessions.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.core.memory_paging.archival_store import ArchivalStore
from nexus.core.memory_paging.context_manager import ContextManager
from nexus.core.memory_paging.namespace_util import strip_tier_prefix
from nexus.core.memory_paging.recall_store import RecallStore

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)

# Max memories to archive in a single _archive_old_recall call
_ARCHIVE_BATCH_LIMIT = 50

# Only run archival check every N adds to main (debounce)
_ARCHIVE_CHECK_INTERVAL = 5

# Cache TTL for get_stats() to avoid repeated COUNT queries (seconds)
_STATS_CACHE_TTL = 5.0


class MemoryPager:
    """Orchestrates 3-tier memory paging system.

    Implements MemGPT's virtual memory management:
    - Main Context: Active working memory (fast access)
    - Recall: Recent history (sequential access)
    - Archival: Long-term knowledge (semantic search)

    Automatically pages memories between tiers based on:
    - Main context capacity (evict when full)
    - Age (move old recall -> archival)
    - Access patterns (promote frequently accessed)

    Thread-safe: Uses session_factory for per-operation sessions.

    Example:
        >>> pager = MemoryPager(session_factory, zone_id="acme")
        >>> pager.add_to_main(memory)  # Automatically handles eviction
        >>> results = pager.search("user preferences")  # Searches all tiers
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        zone_id: str = "default",
        main_capacity: int = 100,
        recall_max_age_hours: float = 24.0,
        warm_up: bool = True,
        vector_db: Any = None,
    ):
        """Initialize memory pager.

        Args:
            session_factory: Callable that returns a new SQLAlchemy session
            zone_id: Zone ID for multi-tenancy
            main_capacity: Max memories in main context
            recall_max_age_hours: Age threshold to move recall -> archival
            warm_up: Load recent memories from DB into context on init
            vector_db: Optional VectorDatabase for pgvector-accelerated archival search
        """
        self._session_factory = session_factory
        self.zone_id = zone_id
        self.recall_max_age_hours = recall_max_age_hours
        self._add_count = 0  # Counter for archival debounce
        self._counter_lock = threading.Lock()  # Protects _add_count

        # Stats cache to avoid repeated COUNT queries
        self._stats_cache: dict | None = None
        self._stats_cache_time: float = 0.0

        # Initialize 3 tiers
        self.context = ContextManager(max_items=main_capacity)
        self.recall = RecallStore(session_factory, zone_id=zone_id)
        self.archival = ArchivalStore(session_factory, zone_id=zone_id, vector_db=vector_db)

        # Warm up context from DB
        if warm_up:
            session = session_factory()
            try:
                self.context.warm_up(session, zone_id, limit=main_capacity)
            finally:
                session.close()

    def add_to_main(self, memory: MemoryModel) -> None:
        """Add memory to main context, handling cascading evictions.

        Flow:
        1. Add to main context
        2. If main full -> evict to recall
        3. Periodically check if recall should age out -> archive old

        Args:
            memory: Memory to add to main context
        """
        # Add to main context (may trigger eviction)
        evicted = self.context.add(memory)

        # Move evicted memories to recall (single transaction for the batch)
        if evicted:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Evicting {len(evicted)} memories from main -> recall")
            self.recall.append_batch(evicted)

        # Thread-safe debounced archival check + cache invalidation
        with self._counter_lock:
            self._stats_cache = None
            self._add_count += 1
            should_archive = self._add_count % _ARCHIVE_CHECK_INTERVAL == 0
            if should_archive:
                self._add_count = 0
        if should_archive:
            self._archive_old_recall()

    def get_from_main(self, memory_id: str) -> MemoryModel | None:
        """Get memory from main context (updates LRU).

        Args:
            memory_id: Memory ID

        Returns:
            Memory if in main context, None otherwise
        """
        return self.context.get(memory_id)

    def search_all_tiers(
        self,
        query_embedding: list[float],
        main_count: int = 5,
        recall_count: int = 3,
        archival_count: int = 2,
        archival_threshold: float = 0.7,
    ) -> dict[str, list[MemoryModel] | list[tuple[MemoryModel, float]]]:
        """Search across all tiers for relevant memories.

        Args:
            query_embedding: Query vector for semantic search
            main_count: Max results from main context
            recall_count: Max results from recall
            archival_count: Max results from archival (semantic search)
            archival_threshold: Minimum similarity for archival

        Returns:
            Dict with results from each tier:
            {
                'main': [MemoryModel, ...],
                'recall': [MemoryModel, ...],
                'archival': [(MemoryModel, score), ...]
            }
        """
        results: dict[str, list[MemoryModel] | list[tuple[MemoryModel, float]]] = {}

        # Get from main context (most recent)
        main_memories = self.context.get_all()[:main_count]
        results["main"] = main_memories

        # Get from recall (recent history)
        recall_memories = self.recall.get_recent(limit=recall_count)
        results["recall"] = recall_memories

        # Search archival (semantic)
        archival_results = self.archival.search_semantic(
            query_embedding=query_embedding,
            threshold=archival_threshold,
            limit=archival_count,
        )
        results["archival"] = archival_results

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"Search results: {len(main_memories)} main, "
                f"{len(recall_memories)} recall, {len(archival_results)} archival"
            )

        return results

    def get_recent_context(self, limit: int = 50) -> list[MemoryModel]:
        """Get recent memories for LLM context.

        Combines main context + recent recall for conversational context.

        Args:
            limit: Total memories to return

        Returns:
            List of recent memories (most recent first)
        """
        # Get all from main
        main_memories = self.context.get_all()

        # Fill remaining from recall
        remaining = limit - len(main_memories)
        if remaining > 0:
            recall_memories = self.recall.get_recent(limit=remaining)
            return main_memories + recall_memories
        else:
            return main_memories[:limit]

    def archive_memory(self, memory_id: str) -> bool:
        """Manually archive a memory (bypass recall tier).

        Args:
            memory_id: Memory ID to archive

        Returns:
            True if archived, False if not found
        """
        # Try to find in main context
        memory = self.context.get(memory_id)
        if memory:
            self.context.remove(memory_id)
            self.archival.store(memory)
            with self._counter_lock:
                self._stats_cache = None
            return True

        # Try recall store - query by memory_id
        session = self._session_factory()
        try:
            from sqlalchemy import select

            from nexus.storage.models import MemoryModel

            stmt = select(MemoryModel).where(MemoryModel.memory_id == memory_id)
            memory = session.execute(stmt).scalar_one_or_none()
            if memory:
                self.recall.remove(memory_id)
                self.archival.store(memory)
                with self._counter_lock:
                    self._stats_cache = None
                return True
        finally:
            session.close()

        return False

    def get_stats(self) -> dict:
        """Get statistics about memory distribution.

        Uses a TTL cache to avoid repeated COUNT queries on recall/archival
        tables. Cache is invalidated on add_to_main() and _archive_old_recall().

        Returns:
            Dict with tier counts and utilization
        """
        now = time.monotonic()
        with self._counter_lock:
            if self._stats_cache is not None and (now - self._stats_cache_time) < _STATS_CACHE_TTL:
                return self._stats_cache

        # COUNT queries run outside lock (they may be slow on large tables)
        main_count = self.context.count()
        recall_count = self.recall.count()
        archival_count = self.archival.count()
        total = main_count + recall_count + archival_count

        stats = {
            "total_memories": total,
            "main": {
                "count": main_count,
                "capacity": self.context.max_items,
                "utilization": main_count / self.context.max_items
                if self.context.max_items > 0
                else 0,
            },
            "recall": {"count": recall_count},
            "archival": {"count": archival_count},
        }

        with self._counter_lock:
            self._stats_cache = stats
            self._stats_cache_time = now
        return stats

    def _archive_old_recall(self) -> None:
        """Move old memories from recall to archival.

        Archives memories older than recall_max_age_hours.
        Uses batch limit to prevent unbounded queries.
        Wraps all archival moves in a single session for atomicity.
        """
        threshold_time = datetime.now(UTC) - timedelta(hours=self.recall_max_age_hours)

        # Get old memories from recall (limited batch)
        old_memories = self.recall.query_temporal(before=threshold_time, limit=_ARCHIVE_BATCH_LIMIT)

        if not old_memories:
            return

        # Archive in batch using a single session for atomicity
        session = self._session_factory()
        try:
            archived_count = 0

            for memory in old_memories:
                # Merge first to get a session-bound copy (handles detached objects)
                merged = session.merge(memory)
                merged.namespace = f"archival/{strip_tier_prefix(merged.namespace)}"
                archived_count += 1

            # Single commit for the entire batch
            session.commit()

            # Invalidate stats cache (tier counts changed)
            with self._counter_lock:
                self._stats_cache = None

            if archived_count > 0:
                logger.info(
                    f"Archived {archived_count} old memories from recall -> archival "
                    f"(older than {self.recall_max_age_hours}h)"
                )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
