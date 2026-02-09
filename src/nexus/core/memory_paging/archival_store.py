"""Archival Store - Tertiary memory tier (semantic search).

Wraps memory_api for semantic/concept-based access patterns.
Optimized for knowledge queries: "What do I know about X?", "Find similar to Y"
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)


class ArchivalStore:
    """Manages archival storage (tertiary memory tier).

    Provides semantic search for long-term knowledge storage.
    Integrates with hierarchical memory consolidation.

    Example:
        >>> archival = ArchivalStore(session, zone_id="acme")
        >>> archival.store(memory)
        >>> results = archival.search_semantic(query_embedding, threshold=0.7)
    """

    def __init__(
        self,
        session: Session,
        zone_id: str = "default",
        namespace: str = "archival",
    ):
        """Initialize archival store.

        Args:
            session: SQLAlchemy session
            zone_id: Zone ID for multi-tenancy
            namespace: Namespace for archival memories (default: "archival")
        """
        self.session = session
        self.zone_id = zone_id
        self.namespace = namespace

        # Import here to avoid circular dependency
        from nexus.core.memory_router import MemoryViewRouter

        self.router = MemoryViewRouter(session)

    def store(self, memory: MemoryModel, trigger_consolidation: bool = True) -> None:
        """Store memory in archival tier.

        Args:
            memory: Memory to archive
            trigger_consolidation: Whether to trigger hierarchical consolidation
        """
        # Update namespace to indicate archival tier
        if not memory.namespace or not memory.namespace.startswith(self.namespace):
            memory.namespace = f"{self.namespace}/{memory.namespace or 'default'}"

        # Ensure memory is in session
        if memory not in self.session:
            self.session.add(memory)

        self.session.commit()

        # TODO: Trigger hierarchical consolidation (Issue #4 from review)
        # For MVP, we skip consolidation and just store atomically
        if trigger_consolidation:
            logger.debug(f"Consolidation not implemented yet for memory {memory.memory_id}")

        logger.debug(f"Stored memory {memory.memory_id} in archival")

    def search_semantic(
        self,
        query_embedding: list[float],
        threshold: float = 0.7,
        limit: int = 10,
        prefer_abstracts: bool = False,  # noqa: ARG002 - Future use with consolidation
    ) -> list[tuple[MemoryModel, float]]:
        """Search archival using semantic similarity.

        Args:
            query_embedding: Query vector for similarity search
            threshold: Minimum similarity score (0-1)
            limit: Maximum results
            prefer_abstracts: Prefer high-level abstracts over atomics

        Returns:
            List of (memory, score) tuples sorted by similarity
        """
        # For MVP, use existing hierarchy retrieval if available
        try:
            # Get all archival memories
            archival_memories = self.router.query_memories(
                zone_id=self.zone_id,
                namespace_prefix=self.namespace,
            )

            if not archival_memories:
                return []

            # Use hierarchy-aware retrieval if consolidation exists
            # For MVP, fallback to simple similarity search
            return self._simple_similarity_search(
                query_embedding, archival_memories, threshold, limit
            )

        except ImportError:
            # Fallback if hierarchy not available
            return self._simple_similarity_search(
                query_embedding,
                self.router.query_memories(
                    zone_id=self.zone_id,
                    namespace_prefix=self.namespace,
                ),
                threshold,
                limit,
            )

    def _simple_similarity_search(
        self,
        query_embedding: list[float],
        memories: list[MemoryModel],
        threshold: float,
        limit: int,
    ) -> list[tuple[MemoryModel, float]]:
        """Simple similarity search without hierarchy.

        Args:
            query_embedding: Query vector
            memories: Memories to search
            threshold: Minimum score
            limit: Maximum results

        Returns:
            List of (memory, score) tuples
        """
        import json

        import numpy as np

        from nexus.core.ace.affinity import compute_cosine_similarity

        query_vec = np.array(query_embedding)
        scored: list[tuple[MemoryModel, float]] = []

        for memory in memories:
            if not memory.embedding:
                continue

            try:
                embedding = json.loads(memory.embedding)
                if not isinstance(embedding, list):
                    continue

                mem_vec = np.array(embedding)
                score = compute_cosine_similarity(query_vec, mem_vec)
                # Normalize to [0, 1]
                score = (score + 1) / 2

                if score >= threshold:
                    scored.append((memory, float(score)))

            except (json.JSONDecodeError, TypeError, ValueError):
                continue

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def count(self) -> int:
        """Get count of memories in archival store."""
        memories = self.router.query_memories(
            zone_id=self.zone_id,
            namespace_prefix=self.namespace,
        )
        return len(memories)

    def get_by_namespace(self, sub_namespace: str) -> list[MemoryModel]:
        """Get all memories in a specific archival sub-namespace.

        Args:
            sub_namespace: Sub-namespace under archival (e.g., "knowledge/facts")

        Returns:
            List of memories
        """
        full_namespace = f"{self.namespace}/{sub_namespace}"
        return self.router.query_memories(
            zone_id=self.zone_id,
            namespace_prefix=full_namespace,
        )

    def remove(self, memory_id: str) -> bool:
        """Remove memory from archival store.

        Args:
            memory_id: Memory ID to remove

        Returns:
            True if removed, False if not found
        """
        return self.router.delete_memory(memory_id)

    def get_newest_timestamp(self) -> datetime | None:
        """Get timestamp of newest memory in archival."""
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        stmt = (
            select(MemoryModel.created_at)
            .where(MemoryModel.zone_id == self.zone_id)
            .where(MemoryModel.namespace.like(f"{self.namespace}%"))
            .order_by(MemoryModel.created_at.desc())
            .limit(1)
        )
        result = self.session.execute(stmt).scalar_one_or_none()
        return result
