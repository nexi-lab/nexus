"""Hierarchical memory abstraction for SimpleMem-style recursive consolidation.

Implements multi-level memory hierarchy where atomic memories are progressively
consolidated into higher-level abstractions while preserving retrieval granularity.

Hierarchy Levels:
    Level 0: Atomic entries (individual facts)
        ↓ cluster by affinity
    Level 1: Clusters (related facts grouped)
        ↓ synthesize abstractions
    Level 2: Abstracts (high-level summaries)
        ↓ further consolidation
    Level N: Meta-abstracts

Issue #1029: Hierarchical memory abstraction (atoms → clusters → abstracts)

References:
    - SimpleMem Paper: https://arxiv.org/abs/2601.02553 (recursive consolidation)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from nexus.services.ace.affinity import (
    AffinityConfig,
    MemoryVector,
    cluster_by_affinity,
    get_cluster_statistics,
)
from nexus.storage.models import MemoryModel

if TYPE_CHECKING:
    from nexus.services.ace.consolidation import ConsolidationEngine

logger = logging.getLogger(__name__)


@dataclass
class HierarchyLevel:
    """Represents a level in the memory hierarchy."""

    level: int
    memories: list[MemoryModel]
    cluster_count: int = 0


@dataclass
class HierarchyResult:
    """Result of building a memory hierarchy."""

    levels: dict[int, HierarchyLevel]
    total_memories: int
    total_abstracts_created: int
    max_level_reached: int
    statistics: dict[str, Any] = field(default_factory=dict)

    @property
    def level_summary(self) -> dict[int, int]:
        """Return count of memories at each level."""
        return {level: len(hl.memories) for level, hl in self.levels.items()}


@dataclass
class HierarchyRetrievalResult:
    """Result of hierarchy-aware retrieval."""

    memories: list[MemoryModel]
    abstracts_used: int
    atomics_used: int
    expanded_from_abstracts: int


class HierarchicalMemoryManager:
    """Manages hierarchical memory abstraction.

    Builds and queries multi-level memory hierarchies where atomic memories
    are progressively consolidated into higher-level abstractions.

    Example:
        >>> from nexus.services.ace.consolidation import ConsolidationEngine
        >>> engine = ConsolidationEngine(session, backend, zone_id)
        >>> manager = HierarchicalMemoryManager(engine, session)
        >>> result = await manager.build_hierarchy_async(memories, max_levels=3)
        >>> print(f"Created {result.total_abstracts_created} abstracts")
    """

    def __init__(
        self,
        consolidation_engine: ConsolidationEngine,
        session: Session,
        zone_id: str = "default",
    ):
        """Initialize the hierarchical memory manager.

        Args:
            consolidation_engine: Engine for consolidating memory clusters
            session: SQLAlchemy session for database operations
            zone_id: Zone ID for multi-zone isolation
        """
        self.engine = consolidation_engine
        self.session = session
        self.zone_id = zone_id

    async def build_hierarchy_async(
        self,
        memories: list[MemoryModel] | None = None,
        memory_ids: list[str] | None = None,
        max_levels: int = 3,
        cluster_threshold: float = 0.6,
        min_cluster_size: int = 2,
        beta: float = 0.7,
        lambda_decay: float = 0.1,
        time_unit_hours: float = 24.0,
    ) -> HierarchyResult:
        """Build memory hierarchy from atomic memories.

        Progressively clusters memories by affinity and consolidates each cluster
        into a higher-level abstraction. Continues until max_levels reached or
        too few memories remain to cluster.

        Args:
            memories: List of MemoryModel objects to build hierarchy from.
                     If None, uses memory_ids to load from database.
            memory_ids: List of memory IDs to load. Ignored if memories provided.
            max_levels: Maximum hierarchy depth (default: 3)
            cluster_threshold: Minimum affinity for clustering (default: 0.6)
            min_cluster_size: Minimum memories per cluster (default: 2)
            beta: Semantic similarity weight in affinity (default: 0.7)
            lambda_decay: Temporal decay rate (default: 0.1)
            time_unit_hours: Time normalization in hours (default: 24.0)

        Returns:
            HierarchyResult with levels, statistics, and metadata

        Raises:
            ValueError: If no memories provided and memory_ids is empty
        """
        # Load memories if not provided
        if memories is None:
            if not memory_ids:
                raise ValueError("Either memories or memory_ids must be provided")
            memories = self._load_memories(memory_ids)

        if len(memories) < min_cluster_size:
            logger.warning(
                f"Too few memories ({len(memories)}) to build hierarchy "
                f"(min_cluster_size={min_cluster_size})"
            )
            return HierarchyResult(
                levels={0: HierarchyLevel(0, memories)},
                total_memories=len(memories),
                total_abstracts_created=0,
                max_level_reached=0,
            )

        # Configure affinity clustering
        config = AffinityConfig(
            beta=beta,
            lambda_decay=lambda_decay,
            time_unit_hours=time_unit_hours,
            cluster_threshold=cluster_threshold,
            min_cluster_size=min_cluster_size,
        )

        # Build hierarchy level by level
        hierarchy: dict[int, HierarchyLevel] = {0: HierarchyLevel(level=0, memories=memories)}
        current_level_memories = memories
        total_abstracts = 0

        for level in range(1, max_levels + 1):
            if len(current_level_memories) < min_cluster_size:
                logger.info(
                    f"Stopping at level {level - 1}: only {len(current_level_memories)} "
                    f"memories (need {min_cluster_size})"
                )
                break

            # Convert to MemoryVector for clustering
            memory_vectors = self._to_memory_vectors(current_level_memories)

            if len(memory_vectors) < min_cluster_size:
                logger.info(
                    f"Stopping at level {level - 1}: only {len(memory_vectors)} "
                    f"memories with embeddings"
                )
                break

            # Cluster by affinity
            cluster_result = cluster_by_affinity(memory_vectors, config)

            if cluster_result.num_clusters == 0:
                logger.info(f"No clusters formed at level {level}")
                break

            # Get cluster statistics for logging
            cluster_stats = get_cluster_statistics(memory_vectors, cluster_result, config)
            if cluster_stats:
                avg_affinity = sum(s.get("avg_affinity", 0) for s in cluster_stats) / len(
                    cluster_stats
                )
                logger.debug(f"Level {level} average cluster affinity: {avg_affinity:.3f}")

            logger.info(
                f"Level {level}: {cluster_result.num_clusters} clusters from "
                f"{len(memory_vectors)} memories"
            )

            # Consolidate each cluster into an abstract
            next_level_memories: list[MemoryModel] = []
            for i, cluster_ids in enumerate(cluster_result.clusters):
                if len(cluster_ids) < min_cluster_size:
                    continue

                # Consolidate cluster
                try:
                    consolidated = await self.engine.consolidate_async(
                        memory_ids=cluster_ids,
                    )

                    if not consolidated.get("consolidated_memory_id"):
                        logger.warning(f"Cluster {i} consolidation failed")
                        continue

                    # Load the new abstract memory
                    abstract_memory = self._get_memory(consolidated["consolidated_memory_id"])
                    if not abstract_memory:
                        continue

                    # Update hierarchy metadata
                    abstract_memory.abstraction_level = level
                    abstract_memory.child_memory_ids = json.dumps(cluster_ids)

                    # Link children to parent and archive them
                    self._link_children_to_parent(cluster_ids, abstract_memory.memory_id)

                    next_level_memories.append(abstract_memory)
                    total_abstracts += 1

                except Exception as e:
                    logger.error(f"Error consolidating cluster {i}: {e}")
                    continue

            if not next_level_memories:
                logger.info(f"No abstracts created at level {level}")
                break

            # Commit changes
            self.session.commit()

            hierarchy[level] = HierarchyLevel(
                level=level,
                memories=next_level_memories,
                cluster_count=cluster_result.num_clusters,
            )

            current_level_memories = next_level_memories

        return HierarchyResult(
            levels=hierarchy,
            total_memories=len(memories),
            total_abstracts_created=total_abstracts,
            max_level_reached=max(hierarchy.keys()),
            statistics={
                "config": {
                    "beta": beta,
                    "lambda_decay": lambda_decay,
                    "cluster_threshold": cluster_threshold,
                    "min_cluster_size": min_cluster_size,
                    "max_levels": max_levels,
                },
                "level_summary": {level: len(hl.memories) for level, hl in hierarchy.items()},
            },
        )

    def retrieve_with_hierarchy(
        self,
        query_embedding: list[float],
        max_results: int = 10,
        include_archived: bool = False,
        prefer_abstracts: bool = True,
        expand_threshold: float = 0.7,
        max_children_per_abstract: int = 2,
    ) -> HierarchyRetrievalResult:
        """Retrieve memories respecting hierarchy.

        Strategy:
        1. First search high-level abstracts
        2. If abstracts match well (above expand_threshold), expand to children
        3. Fill remaining slots with atomic memories

        Args:
            query_embedding: Query vector for similarity search
            max_results: Maximum memories to return (default: 10)
            include_archived: Include archived memories (default: False)
            prefer_abstracts: Prefer higher-level abstracts (default: True)
            expand_threshold: Score threshold to expand abstract to children (default: 0.7)
            max_children_per_abstract: Max children to include per abstract (default: 2)

        Returns:
            HierarchyRetrievalResult with memories and metadata
        """
        results: list[MemoryModel] = []
        abstracts_used = 0
        expanded_count = 0

        if prefer_abstracts:
            # Search high-level abstracts first (level >= 2)
            abstracts = self._search_by_level(
                query_embedding,
                min_level=2,
                limit=max_results // 2,
                include_archived=include_archived,
            )

            for abstract, score in abstracts:
                if len(results) >= max_results:
                    break

                results.append(abstract)
                abstracts_used += 1

                # Expand to children if score is high enough
                if score >= expand_threshold:
                    children = self._get_children(abstract, limit=max_children_per_abstract)
                    for child in children:
                        if len(results) < max_results and child not in results:
                            results.append(child)
                            expanded_count += 1

        # Fill remaining with atomics
        remaining = max_results - len(results)
        if remaining > 0:
            atomics = self._search_by_level(
                query_embedding,
                min_level=0,
                max_level=1,
                limit=remaining,
                include_archived=include_archived,
                exclude_ids=[m.memory_id for m in results],
            )
            for atomic, _ in atomics:
                results.append(atomic)

        return HierarchyRetrievalResult(
            memories=results,
            abstracts_used=abstracts_used,
            atomics_used=len(results) - abstracts_used - expanded_count,
            expanded_from_abstracts=expanded_count,
        )

    def get_hierarchy_for_memory(
        self, memory_id: str, include_children: bool = True
    ) -> dict[str, Any]:
        """Get the full hierarchy tree for a memory.

        Args:
            memory_id: ID of the memory to get hierarchy for
            include_children: Include child memories recursively (default: True)

        Returns:
            Dictionary with memory and its hierarchy relationships
        """
        memory = self._get_memory(memory_id)
        if not memory:
            return {}

        result: dict[str, Any] = {
            "memory_id": memory.memory_id,
            "abstraction_level": memory.abstraction_level,
            "is_archived": memory.is_archived,
            "parent_memory_id": memory.parent_memory_id,
        }

        # Get parent chain (ancestors)
        ancestors = []
        current = memory
        while current.parent_memory_id:
            parent = self._get_memory(current.parent_memory_id)
            if not parent:
                break
            ancestors.append(
                {
                    "memory_id": parent.memory_id,
                    "abstraction_level": parent.abstraction_level,
                }
            )
            current = parent
        result["ancestors"] = ancestors

        # Get children recursively
        if include_children:
            result["children"] = self._get_children_recursive(memory)

        return result

    # -------------------------------------------------------------------------
    # Private helper methods
    # -------------------------------------------------------------------------

    def _load_memories(self, memory_ids: list[str]) -> list[MemoryModel]:
        """Load memories from database by IDs."""
        stmt = select(MemoryModel).where(
            MemoryModel.memory_id.in_(memory_ids),
            MemoryModel.zone_id == self.zone_id,
        )
        result = self.session.execute(stmt)
        return list(result.scalars().all())

    def _get_memory(self, memory_id: str) -> MemoryModel | None:
        """Get a single memory by ID."""
        stmt = select(MemoryModel).where(
            MemoryModel.memory_id == memory_id,
            MemoryModel.zone_id == self.zone_id,
        )
        result = self.session.execute(stmt)
        return result.scalar_one_or_none()

    def _to_memory_vectors(self, memories: list[MemoryModel]) -> list[MemoryVector]:
        """Convert MemoryModel objects to MemoryVector for clustering.

        Only includes memories that have embeddings.
        """
        vectors = []
        for memory in memories:
            if not memory.embedding:
                continue

            # Parse embedding from JSON
            try:
                embedding = json.loads(memory.embedding)
                if not isinstance(embedding, list):
                    continue
            except (json.JSONDecodeError, TypeError):
                continue

            vectors.append(
                MemoryVector(
                    memory_id=memory.memory_id,
                    embedding=embedding,
                    created_at=memory.created_at or datetime.now(UTC),
                    content=None,  # Not needed for clustering
                    importance=memory.importance,
                    memory_type=memory.memory_type,
                )
            )

        return vectors

    def _link_children_to_parent(self, child_ids: list[str], parent_id: str) -> None:
        """Link child memories to parent and archive them."""
        stmt = (
            update(MemoryModel)
            .where(
                MemoryModel.memory_id.in_(child_ids),
                MemoryModel.zone_id == self.zone_id,
            )
            .values(parent_memory_id=parent_id, is_archived=True)
        )
        self.session.execute(stmt)

    def _get_children(self, memory: MemoryModel, limit: int | None = None) -> list[MemoryModel]:
        """Get direct children of a memory."""
        if not memory.child_memory_ids:
            return []

        try:
            child_ids = json.loads(memory.child_memory_ids)
            if not isinstance(child_ids, list):
                return []
        except (json.JSONDecodeError, TypeError):
            return []

        stmt = select(MemoryModel).where(
            MemoryModel.memory_id.in_(child_ids),
            MemoryModel.zone_id == self.zone_id,
        )
        if limit:
            stmt = stmt.limit(limit)

        result = self.session.execute(stmt)
        return list(result.scalars().all())

    def _get_children_recursive(
        self, memory: MemoryModel, max_depth: int = 10
    ) -> list[dict[str, Any]]:
        """Get children recursively with depth limit."""
        if max_depth <= 0:
            return []

        children = self._get_children(memory)
        result = []

        for child in children:
            child_data: dict[str, Any] = {
                "memory_id": child.memory_id,
                "abstraction_level": child.abstraction_level,
                "is_archived": child.is_archived,
            }
            grandchildren = self._get_children_recursive(child, max_depth - 1)
            if grandchildren:
                child_data["children"] = grandchildren
            result.append(child_data)

        return result

    def _search_by_level(
        self,
        query_embedding: list[float],
        min_level: int = 0,
        max_level: int | None = None,
        limit: int = 10,
        include_archived: bool = False,
        exclude_ids: list[str] | None = None,
    ) -> list[tuple[MemoryModel, float]]:
        """Search memories by abstraction level.

        Returns list of (memory, score) tuples sorted by similarity.

        Note: This is a simplified implementation. Production use should
        leverage vector similarity search (pgvector, etc.).
        """
        import numpy as np

        from nexus.services.ace.affinity import compute_cosine_similarity

        # Build query
        stmt = select(MemoryModel).where(
            MemoryModel.zone_id == self.zone_id,
            MemoryModel.abstraction_level >= min_level,
            MemoryModel.embedding.isnot(None),
        )

        if max_level is not None:
            stmt = stmt.where(MemoryModel.abstraction_level <= max_level)

        if not include_archived:
            stmt = stmt.where(MemoryModel.is_archived == False)  # noqa: E712

        if exclude_ids:
            stmt = stmt.where(MemoryModel.memory_id.notin_(exclude_ids))

        result = self.session.execute(stmt)
        memories = list(result.scalars().all())

        # Compute similarities
        query_vec = np.array(query_embedding)
        scored: list[tuple[MemoryModel, float]] = []

        for memory in memories:
            try:
                embedding = json.loads(memory.embedding)  # type: ignore
                if not isinstance(embedding, list):
                    continue
                mem_vec = np.array(embedding)
                score = compute_cosine_similarity(query_vec, mem_vec)
                # Normalize to [0, 1]
                score = (score + 1) / 2
                scored.append((memory, float(score)))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


# Synchronous wrapper for non-async contexts
def build_hierarchy(
    consolidation_engine: ConsolidationEngine,
    session: Session,
    memories: list[MemoryModel] | None = None,
    memory_ids: list[str] | None = None,
    zone_id: str = "default",
    **kwargs: Any,
) -> HierarchyResult:
    """Synchronous wrapper for build_hierarchy_async.

    Args:
        consolidation_engine: Engine for consolidating memory clusters
        session: SQLAlchemy session
        memories: List of MemoryModel objects
        memory_ids: List of memory IDs (if memories not provided)
        zone_id: Zone ID for multi-zone isolation
        **kwargs: Additional arguments passed to build_hierarchy_async

    Returns:
        HierarchyResult with hierarchy data
    """
    from nexus.core.sync_bridge import run_sync

    manager = HierarchicalMemoryManager(consolidation_engine, session, zone_id)
    return run_sync(
        manager.build_hierarchy_async(memories=memories, memory_ids=memory_ids, **kwargs)
    )
