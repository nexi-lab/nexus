"""Memory API with MemGPT 3-tier paging (Issue #1258).

Extends the existing Memory API with automatic paging between:
- Main Context (working memory)
- Recall Storage (recent history)
- Archival Storage (long-term knowledge)

Usage:
    >>> from nexus.core.memory_with_paging import MemoryWithPaging
    >>> memory = MemoryWithPaging(session, backend, zone_id="acme", user_id="alice")
    >>> memory.store("Important fact", enable_paging=True)
    >>> results = memory.search_with_paging("query about fact")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.core.memory_api import Memory
from nexus.core.memory_paging import MemoryPager

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.backends.base import Backend
    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)


class MemoryWithPaging(Memory):
    """Memory API with MemGPT 3-tier paging.

    Drop-in replacement for Memory API that adds automatic paging.

    Example:
        >>> # Instead of:
        >>> memory = Memory(session, backend, zone_id="acme")
        >>>
        >>> # Use:
        >>> memory = MemoryWithPaging(session, backend, zone_id="acme")
        >>> # Same API, but with automatic paging!
    """

    def __init__(
        self,
        session: Session,
        backend: Backend,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        entity_registry=None,
        enable_paging: bool = True,
        main_capacity: int = 100,
        recall_max_age_hours: float = 24.0,
    ):
        """Initialize Memory API with paging.

        Args:
            session: SQLAlchemy session
            backend: Content storage backend
            zone_id: Zone ID for multi-tenancy
            user_id: User ID
            agent_id: Agent ID
            entity_registry: Entity registry
            enable_paging: Enable 3-tier paging (default: True)
            main_capacity: Max memories in main context (default: 100)
            recall_max_age_hours: Age threshold for archival (default: 24h)
        """
        super().__init__(
            session=session,
            backend=backend,
            zone_id=zone_id,
            user_id=user_id,
            agent_id=agent_id,
            entity_registry=entity_registry,
        )

        self.enable_paging = enable_paging

        if enable_paging:
            self.pager = MemoryPager(
                session=session,
                zone_id=zone_id or "default",
                main_capacity=main_capacity,
                recall_max_age_hours=recall_max_age_hours,
            )
            logger.info(
                f"MemGPT 3-tier paging enabled: "
                f"main_capacity={main_capacity}, "
                f"recall_max_age={recall_max_age_hours}h"
            )
        else:
            self.pager = None

    def store(
        self,
        content: str | bytes,
        scope: str = "agent",
        memory_type: str | None = None,
        importance: float | None = None,
        namespace: str | None = None,
        auto_page: bool = True,
        **kwargs: Any,
    ) -> str:
        """Store memory with automatic paging.

        Args:
            content: Memory content
            scope: Memory scope
            memory_type: Memory type
            importance: Importance score (0-1)
            namespace: Hierarchical namespace
            auto_page: Automatically page to main context (default: True)
            **kwargs: Additional arguments passed to parent store()

        Returns:
            Memory ID
        """
        # Store using parent API
        memory_id = super().store(
            content=content,
            scope=scope,
            memory_type=memory_type,
            importance=importance,
            namespace=namespace,
            **kwargs,
        )

        # Add to paging system if enabled
        if self.enable_paging and auto_page and self.pager:
            # Load the memory
            memory = self.memory_router.get_memory_by_id(memory_id)
            if memory:
                self.pager.add_to_main(memory)
                logger.debug(f"Added memory {memory_id} to main context via paging")

        return memory_id

    def search_with_paging(
        self,
        query: str,
        main_count: int = 5,
        recall_count: int = 3,
        archival_count: int = 2,
        archival_threshold: float = 0.7,
    ) -> dict[str, list]:
        """Search across all memory tiers.

        Args:
            query: Search query
            main_count: Max results from main context
            recall_count: Max results from recall
            archival_count: Max results from archival
            archival_threshold: Minimum similarity for archival

        Returns:
            Dict with results from each tier
        """
        if not self.enable_paging or not self.pager:
            raise ValueError("Paging not enabled. Use enable_paging=True")

        # Get embedding for query
        query_embedding = self._get_embedding(query)

        # Search all tiers
        return self.pager.search_all_tiers(
            query_embedding=query_embedding,
            main_count=main_count,
            recall_count=recall_count,
            archival_count=archival_count,
            archival_threshold=archival_threshold,
        )

    def get_recent_context(self, limit: int = 50) -> list[dict]:
        """Get recent memories for LLM context.

        Args:
            limit: Max memories to return

        Returns:
            List of memory dicts (most recent first)
        """
        if not self.enable_paging or not self.pager:
            # Fallback to regular query
            memories = self.query(limit=limit)
            return memories

        # Use paging system
        memories = self.pager.get_recent_context(limit=limit)
        return [self._memory_to_dict(m) for m in memories]

    def get_paging_stats(self) -> dict:
        """Get memory distribution across tiers.

        Returns:
            Dict with tier statistics
        """
        if not self.enable_paging or not self.pager:
            return {"paging_enabled": False}

        stats = self.pager.get_stats()
        stats["paging_enabled"] = True
        return stats

    def _get_embedding(self, text: str) -> list[float]:
        """Get embedding for text (placeholder for real implementation).

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        # TODO: Integrate with real embedding service
        # For now, return dummy embedding
        import hashlib

        # Generate deterministic dummy embedding from hash
        hash_val = int(hashlib.sha256(text.encode()).hexdigest(), 16)
        dim = 1536  # OpenAI embedding dimension
        return [(hash_val >> (i % 256)) % 100 / 100.0 for i in range(dim)]

    def _memory_to_dict(self, memory: MemoryModel) -> dict:
        """Convert MemoryModel to dict.

        Args:
            memory: Memory model

        Returns:
            Memory as dict
        """
        return {
            "memory_id": memory.memory_id,
            "content_hash": memory.content_hash,
            "zone_id": memory.zone_id,
            "user_id": memory.user_id,
            "agent_id": memory.agent_id,
            "scope": memory.scope,
            "memory_type": memory.memory_type,
            "importance": memory.importance,
            "namespace": memory.namespace,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
        }
