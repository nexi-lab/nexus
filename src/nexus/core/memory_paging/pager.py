"""Memory Pager - Orchestrates 3-tier memory system.

Manages memory flow between main context, recall, and archival tiers.
Automatically pages memories based on capacity and age.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from nexus.core.memory_paging.archival_store import ArchivalStore
from nexus.core.memory_paging.context_manager import ContextManager
from nexus.core.memory_paging.recall_store import RecallStore

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)


class MemoryPager:
    """Orchestrates 3-tier memory paging system.

    Implements MemGPT's virtual memory management:
    - Main Context: Active working memory (fast access)
    - Recall: Recent history (sequential access)
    - Archival: Long-term knowledge (semantic search)

    Automatically pages memories between tiers based on:
    - Main context capacity (evict when full)
    - Age (move old recall → archival)
    - Access patterns (promote frequently accessed)

    Example:
        >>> pager = MemoryPager(session, zone_id="acme")
        >>> pager.add_to_main(memory)  # Automatically handles eviction
        >>> results = pager.search("user preferences")  # Searches all tiers
    """

    def __init__(
        self,
        session: Session,
        zone_id: str = "default",
        main_capacity: int = 100,
        recall_max_age_hours: float = 24.0,
    ):
        """Initialize memory pager.

        Args:
            session: SQLAlchemy session
            zone_id: Zone ID for multi-tenancy
            main_capacity: Max memories in main context
            recall_max_age_hours: Age threshold to move recall → archival
        """
        self.session = session
        self.zone_id = zone_id
        self.recall_max_age_hours = recall_max_age_hours

        # Initialize 3 tiers
        self.context = ContextManager(max_items=main_capacity)
        self.recall = RecallStore(session, zone_id=zone_id)
        self.archival = ArchivalStore(session, zone_id=zone_id)

    def add_to_main(self, memory: MemoryModel) -> None:
        """Add memory to main context, handling cascading evictions.

        Flow:
        1. Add to main context
        2. If main full → evict to recall
        3. Check if recall should age out → archive old

        Args:
            memory: Memory to add to main context
        """
        # Add to main context (may trigger eviction)
        evicted = self.context.add(memory)

        # Move evicted memories to recall
        for evicted_memory in evicted:
            self.recall.append(evicted_memory)
            logger.debug(
                f"Evicted {evicted_memory.memory_id} from main → recall "
                f"({self.recall.count()} in recall)"
            )

        # Check if recall should be archived
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
    ) -> dict[str, list]:
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
        results = {}

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
            return True

        # Try recall store
        memory = self.session.get(memory_id)
        if memory:
            self.recall.remove(memory_id)
            self.archival.store(memory)
            return True

        return False

    def get_stats(self) -> dict:
        """Get statistics about memory distribution.

        Returns:
            Dict with tier counts and utilization
        """
        main_count = self.context.count()
        recall_count = self.recall.count()
        archival_count = self.archival.count()
        total = main_count + recall_count + archival_count

        return {
            "total_memories": total,
            "main": {
                "count": main_count,
                "capacity": self.context.max_items,
                "utilization": main_count / self.context.max_items if self.context.max_items > 0 else 0,
            },
            "recall": {"count": recall_count},
            "archival": {"count": archival_count},
        }

    def _archive_old_recall(self) -> None:
        """Move old memories from recall to archival.

        Archives memories older than recall_max_age_hours.
        """
        threshold_time = datetime.now(UTC) - timedelta(hours=self.recall_max_age_hours)

        # Get old memories from recall
        old_memories = self.recall.query_temporal(before=threshold_time)

        archived_count = 0
        for memory in old_memories:
            # Move to archival
            self.archival.store(memory, trigger_consolidation=False)
            self.recall.remove(memory.memory_id)
            archived_count += 1

        if archived_count > 0:
            logger.info(
                f"Archived {archived_count} old memories from recall → archival "
                f"(older than {self.recall_max_age_hours}h)"
            )
