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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nexus.core.memory_api import Memory
from nexus.core.memory_paging import MemoryPager

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.backends.base import Backend
    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)

# Default embedding dimension (OpenAI ada-002)
_EMBEDDING_DIM = 1536


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
        entity_registry: Any = None,
        enable_paging: bool = True,
        main_capacity: int = 100,
        recall_max_age_hours: float = 24.0,
        warm_up: bool = True,
        vector_db: Any = None,
        engine: Any = None,
        session_factory: Callable[[], Session] | None = None,
    ):
        """Initialize Memory API with paging.

        Args:
            session: SQLAlchemy session (used by parent Memory class)
            backend: Content storage backend
            zone_id: Zone ID for multi-tenancy
            user_id: User ID
            agent_id: Agent ID
            entity_registry: Entity registry
            enable_paging: Enable 3-tier paging (default: True)
            main_capacity: Max memories in main context (default: 100)
            recall_max_age_hours: Age threshold for archival (default: 24h)
            warm_up: Load recent memories from DB into context on init
            vector_db: Optional VectorDatabase for pgvector-accelerated archival search
            engine: Optional SQLAlchemy engine (creates VectorDatabase if vector_db not provided)
            session_factory: Session factory for thread-safe pager operations.
                If not provided, falls back to a lambda returning the passed session.
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
        self.pager: MemoryPager | None

        # Build session factory: prefer explicit, else derive from session
        if session_factory is None:
            # Fallback: wrap the provided session (single-threaded use)
            session_factory = lambda: session  # noqa: E731

        # Create VectorDatabase from engine if not provided directly
        if vector_db is None and engine is not None:
            try:
                from nexus.search.vector_db import VectorDatabase

                vector_db = VectorDatabase(engine)
                vector_db.initialize()
            except Exception as e:
                logger.warning(f"Failed to initialize VectorDatabase: {e}")
                vector_db = None

        if enable_paging:
            self.pager = MemoryPager(
                session_factory=session_factory,
                zone_id=zone_id or "default",
                main_capacity=main_capacity,
                recall_max_age_hours=recall_max_age_hours,
                warm_up=warm_up,
                vector_db=vector_db,
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
        content: str | bytes | dict[str, Any],
        scope: str = "user",
        memory_type: str | None = None,
        importance: float | None = None,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        _metadata: dict[str, Any] | None = None,
        context: Any = None,
        generate_embedding: bool = True,
        embedding_provider: Any = None,
        resolve_coreferences: bool = False,
        coreference_context: str | None = None,
        resolve_temporal: bool = False,
        temporal_reference_time: Any = None,
        extract_entities: bool = True,
        extract_temporal: bool = True,
        extract_relationships: bool = False,
        relationship_types: list[str] | None = None,
        store_to_graph: bool = False,
        valid_at: Any = None,
        classify_stability: bool = True,  # #1191: Auto-classify temporal stability
        auto_page: bool = True,  # Memory paging specific parameter
    ) -> str:
        """Store memory with automatic paging.

        Args:
            content: Memory content
            scope: Memory scope
            memory_type: Memory type
            importance: Importance score (0-1)
            namespace: Hierarchical namespace
            path_key: Optional key for upsert mode
            state: Memory state ('inactive', 'active')
            _metadata: Additional metadata
            context: Operation context
            generate_embedding: Generate embedding for semantic search
            embedding_provider: Optional embedding provider
            resolve_coreferences: Resolve pronouns to entity names
            coreference_context: Prior conversation context
            resolve_temporal: Resolve temporal expressions to absolute dates
            temporal_reference_time: Reference time for temporal resolution
            extract_entities: Extract named entities
            extract_temporal: Extract temporal metadata for date queries
            extract_relationships: Extract relationships (triplets)
            relationship_types: Custom relationship types
            store_to_graph: Store entities/relationships to graph tables
            valid_at: When fact became valid in real world
            classify_stability: Auto-classify temporal stability (default: True)
            auto_page: Automatically page to main context (default: True)

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
            path_key=path_key,
            state=state,
            _metadata=_metadata,
            context=context,
            generate_embedding=generate_embedding,
            embedding_provider=embedding_provider,
            resolve_coreferences=resolve_coreferences,
            coreference_context=coreference_context,
            resolve_temporal=resolve_temporal,
            temporal_reference_time=temporal_reference_time,
            extract_entities=extract_entities,
            extract_temporal=extract_temporal,
            extract_relationships=extract_relationships,
            relationship_types=relationship_types,
            store_to_graph=store_to_graph,
            valid_at=valid_at,
            classify_stability=classify_stability,
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

    def get_recent_context(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent memories for LLM context.

        Args:
            limit: Max memories to return

        Returns:
            List of memory dicts (most recent first)
        """
        if not self.enable_paging or not self.pager:
            # Fallback to regular query
            return self.query(limit=limit)

        # Use paging system
        memory_models = self.pager.get_recent_context(limit=limit)
        return [self._memory_to_dict(m) for m in memory_models]

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

        logger.warning(
            "Using dummy embedding - archival semantic search will return random results. "
            "Integrate a real embedding provider for production use."
        )

        # Generate deterministic dummy embedding from hash
        hash_val = int(hashlib.sha256(text.encode()).hexdigest(), 16)
        return [(hash_val >> (i % 256)) % 100 / 100.0 for i in range(_EMBEDDING_DIM)]

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
