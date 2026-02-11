"""Archival Store - Tertiary memory tier (semantic search).

Wraps memory_api for semantic/concept-based access patterns.
Optimized for knowledge queries: "What do I know about X?", "Find similar to Y"

Thread-safe: Each operation creates its own session from the session factory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.core.memory_paging.namespace_util import strip_tier_prefix

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)


# Max memories to load for Python-side similarity search.
# Prevents O(n) full-table scan when pgvector is unavailable.
_PYTHON_SEARCH_CAP = 1000


class ArchivalStore:
    """Manages archival storage (tertiary memory tier).

    Provides semantic search for long-term knowledge storage.
    Integrates with hierarchical memory consolidation.

    Thread-safe: Uses session_factory to create per-operation sessions.

    Example:
        >>> archival = ArchivalStore(session_factory, zone_id="acme")
        >>> archival.store(memory)
        >>> results = archival.search_semantic(query_embedding, threshold=0.7)
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        zone_id: str = "default",
        namespace: str = "archival",
        vector_db: Any = None,
    ):
        """Initialize archival store.

        Args:
            session_factory: Callable that returns a new SQLAlchemy session
            zone_id: Zone ID for multi-tenancy
            namespace: Namespace for archival memories (default: "archival")
            vector_db: Optional VectorDatabase for pgvector-accelerated search
        """
        self._session_factory = session_factory
        self.zone_id = zone_id
        self.namespace = namespace
        self._vector_db = vector_db

    def store(self, memory: MemoryModel, trigger_consolidation: bool = True) -> None:
        """Store memory in archival tier.

        Merges the (possibly detached) memory into a fresh session, updates
        its namespace to the archival tier, and commits.

        Args:
            memory: Memory to archive
            trigger_consolidation: Whether to trigger hierarchical consolidation
        """
        session = self._session_factory()
        try:
            # Merge first to get a session-bound copy (handles detached objects)
            merged = session.merge(memory)
            merged.namespace = f"{self.namespace}/{strip_tier_prefix(merged.namespace)}"

            session.commit()

            # TODO: Trigger hierarchical consolidation (Issue #4 from review)
            if trigger_consolidation and logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Consolidation not implemented yet for memory {merged.memory_id}")

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Stored memory {merged.memory_id} in archival")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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
        # Try database-accelerated search (pgvector) first
        if self._vector_db and getattr(self._vector_db, "vec_available", False):
            try:
                return self._db_vector_search(query_embedding, threshold, limit)
            except Exception as e:
                logger.warning(f"DB vector search failed, falling back to Python: {e}")

        # Fallback to Python-based similarity search (capped to prevent O(n) full scan)
        session = self._session_factory()
        try:
            from nexus.core.memory_router import MemoryViewRouter

            router = MemoryViewRouter(session)
            archival_memories = router.query_memories(
                zone_id=self.zone_id,
                namespace_prefix=self.namespace,
                limit=_PYTHON_SEARCH_CAP,
            )

            if not archival_memories:
                return []

            if len(archival_memories) >= _PYTHON_SEARCH_CAP:
                logger.warning(
                    f"Archival Python search capped at {_PYTHON_SEARCH_CAP} memories. "
                    f"Enable pgvector for full semantic search over large archives."
                )

            return self._simple_similarity_search(
                query_embedding, archival_memories, threshold, limit
            )
        finally:
            session.close()

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

    def _db_vector_search(
        self,
        query_embedding: list[float],
        threshold: float,
        limit: int,
    ) -> list[tuple[MemoryModel, float]]:
        """Database-accelerated vector search on memories table.

        Uses pgvector (<=> operator) for PostgreSQL.
        Falls back to Python search for SQLite (embeddings stored as JSON text).

        Args:
            query_embedding: Query vector
            threshold: Minimum similarity score (0-1)
            limit: Maximum results

        Returns:
            List of (memory, score) tuples sorted by similarity
        """
        session = self._session_factory()
        try:
            from sqlalchemy import text

            from nexus.storage.models import MemoryModel

            db_type = self._vector_db.db_type

            if db_type == "postgresql":
                stmt = text("""
                    SELECT memory_id,
                           1 - (embedding::vector <=> CAST(:query AS vector)) as similarity
                    FROM memories
                    WHERE zone_id = :zone_id
                      AND namespace LIKE :ns_prefix || '%'
                      AND state = 'active'
                      AND embedding IS NOT NULL
                    ORDER BY embedding::vector <=> CAST(:query AS vector)
                    LIMIT :limit
                """)
                rows = session.execute(
                    stmt,
                    {
                        "query": str(query_embedding),
                        "zone_id": self.zone_id,
                        "ns_prefix": self.namespace,
                        "limit": limit,
                    },
                ).fetchall()

                results: list[tuple[MemoryModel, float]] = []
                for row in rows:
                    if row.similarity >= threshold:
                        memory = session.get(MemoryModel, row.memory_id)
                        if memory:
                            results.append((memory, float(row.similarity)))
                return results

            # For SQLite, embeddings are stored as JSON text, not sqlite-vec blobs.
            # Fall back to Python-based search (capped).
            from nexus.core.memory_router import MemoryViewRouter

            router = MemoryViewRouter(session)
            archival_memories = router.query_memories(
                zone_id=self.zone_id,
                namespace_prefix=self.namespace,
                limit=_PYTHON_SEARCH_CAP,
            )
            return self._simple_similarity_search(
                query_embedding, archival_memories, threshold, limit
            )
        finally:
            session.close()

    def count(self) -> int:
        """Get count of memories in archival store."""
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

    def get_by_namespace(self, sub_namespace: str) -> list[MemoryModel]:
        """Get all memories in a specific archival sub-namespace.

        Args:
            sub_namespace: Sub-namespace under archival (e.g., "knowledge/facts")

        Returns:
            List of memories
        """
        session = self._session_factory()
        try:
            from nexus.core.memory_router import MemoryViewRouter

            full_namespace = f"{self.namespace}/{sub_namespace}"
            router = MemoryViewRouter(session)
            return router.query_memories(
                zone_id=self.zone_id,
                namespace_prefix=full_namespace,
            )
        finally:
            session.close()

    def remove(self, memory_id: str) -> bool:
        """Remove memory from archival store.

        Args:
            memory_id: Memory ID to remove

        Returns:
            True if removed, False if not found
        """
        session = self._session_factory()
        try:
            from nexus.core.memory_router import MemoryViewRouter

            router = MemoryViewRouter(session)
            return router.delete_memory(memory_id)
        finally:
            session.close()

    def get_newest_timestamp(self) -> datetime | None:
        """Get timestamp of newest active memory in archival."""
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
                .order_by(MemoryModel.created_at.desc())
                .limit(1)
            )
            return session.execute(stmt).scalar_one_or_none()
        finally:
            session.close()
