"""Memory consolidation engine for importance-based merging.

Supports two consolidation strategies:
1. Batch-based: Consolidate memories in arbitrary batches by criteria
2. Affinity-based: Cluster memories by semantic + temporal affinity (Issue #1026)

The affinity-based approach uses SimpleMem-inspired scoring:
    affinity = beta * cos(v_i, v_j) + (1 - beta) * exp(-lambda * |t_i - t_j|)

Reference: SimpleMem: Efficient Lifelong Memory for LLM Agents
https://arxiv.org/html/2601.02553
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from nexus.llm.message import Message, MessageRole
from nexus.llm.provider import LLMProvider
from nexus.services.ace.affinity import (
    AffinityConfig,
    MemoryVector,
    cluster_by_affinity,
    get_cluster_statistics,
)
from nexus.storage.models import MemoryModel

if TYPE_CHECKING:
    from nexus.search.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)


def _run_coroutine(coro: Any) -> Any:
    """Run a coroutine from synchronous code.

    Uses run_sync() which handles both sync and async contexts
    (no running loop -> asyncio.run, running loop -> background thread).

    Args:
        coro: Coroutine to execute.

    Returns:
        The coroutine's return value.
    """
    from nexus.core.sync_bridge import run_sync

    return run_sync(coro)


class ConsolidationEngine:
    """Consolidate memories based on importance and similarity.

    Implements intelligent memory consolidation to prevent context overflow
    by merging related low-importance memories into high-importance summaries.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        llm_provider: LLMProvider,
        user_id: str,
        agent_id: str | None = None,
        zone_id: str | None = None,
        session_factory: Any | None = None,
    ):
        """Initialize consolidation engine.

        Args:
            session: Database session
            backend: Storage backend for CAS content
            llm_provider: LLM provider for consolidation
            user_id: User ID for ownership
            agent_id: Optional agent ID
            zone_id: Optional zone ID
            session_factory: Callable that returns a new Session (for thread safety)
        """
        self.session = session
        self._session_factory = session_factory or (lambda: Session(bind=session.get_bind()))
        self.backend = backend
        self.llm_provider = llm_provider
        self.user_id = user_id
        self.agent_id = agent_id
        self.zone_id = zone_id

    async def consolidate_async(
        self,
        memory_ids: list[str],
        importance_threshold: float = 0.5,
        max_consolidated_memories: int = 10,
    ) -> dict[str, Any]:
        """Consolidate multiple memories into a summary (async).

        Args:
            memory_ids: List of memory IDs to consolidate
            importance_threshold: Only consolidate memories below this importance
            max_consolidated_memories: Maximum memories to include in one consolidation

        Returns:
            Dictionary with consolidation results:
                - consolidated_memory_id: ID of new consolidated memory
                - source_memory_ids: List of source memory IDs
                - memories_consolidated: Number of memories consolidated
                - importance_score: Importance of consolidated memory

        Example:
            >>> result = await consolidation_engine.consolidate_async(
            ...     memory_ids=["mem_1", "mem_2", "mem_3"],
            ...     importance_threshold=0.6
            ... )
            >>> print(f"Consolidated {result['memories_consolidated']} memories")
        """
        # Load memories
        memories = []
        for memory_id in memory_ids[:max_consolidated_memories]:
            memory_data = self._load_memory(memory_id)
            if memory_data and memory_data.get("importance", 0.0) < importance_threshold:
                memories.append(memory_data)

        if len(memories) < 2:
            raise ValueError("Need at least 2 memories to consolidate")

        # Build consolidation prompt
        prompt = self._build_consolidation_prompt(memories)

        # Call LLM for consolidation
        messages = [Message(role=MessageRole.USER, content=prompt)]
        response = await self.llm_provider.complete_async(messages)
        consolidated_text = response.content

        # Calculate importance (max of source memories + bonus)
        max_importance = max(m.get("importance", 0.0) for m in memories)
        consolidated_importance = min(max_importance + 0.1, 1.0)

        # Store consolidated memory
        consolidated_memory_id = self._store_consolidated_memory(
            memories,
            consolidated_text or "",
            consolidated_importance,
        )

        # Mark source memories as consolidated
        self._mark_memories_consolidated(
            [m["memory_id"] for m in memories],
            consolidated_memory_id,
        )

        return {
            "consolidated_memory_id": consolidated_memory_id,
            "source_memory_ids": [m["memory_id"] for m in memories],
            "memories_consolidated": len(memories),
            "importance_score": consolidated_importance,
        }

    def consolidate_by_criteria(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace
        namespace_prefix: str | None = None,  # v0.8.0: Namespace prefix
        importance_max: float = 0.5,
        batch_size: int = 10,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Consolidate memories matching criteria.

        Args:
            memory_type: Filter by memory type
            scope: Filter by scope
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix. v0.8.0
            importance_max: Only consolidate memories with importance <= this
            batch_size: Number of memories to consolidate per batch
            limit: Maximum total memories to process

        Returns:
            List of consolidation results
        """
        # Query candidate memories
        query = self.session.query(MemoryModel).filter(
            MemoryModel.agent_id == self.agent_id,
            MemoryModel.importance <= importance_max,
            MemoryModel.consolidated_from.is_(None),  # Not already consolidated
        )

        # Ownership filters (match _query_candidate_memories)
        if self.user_id:
            query = query.filter(MemoryModel.user_id == self.user_id)
        if self.zone_id:
            query = query.filter(MemoryModel.zone_id == self.zone_id)

        if memory_type:
            query = query.filter_by(memory_type=memory_type)
        if scope:
            query = query.filter_by(scope=scope)

        # v0.8.0: Namespace filtering
        if namespace:
            query = query.filter_by(namespace=namespace)
        elif namespace_prefix:
            safe_prefix = namespace_prefix.replace("%", r"\%").replace("_", r"\_")
            query = query.filter(MemoryModel.namespace.like(f"{safe_prefix}%"))

        query = query.order_by(MemoryModel.created_at.desc()).limit(limit)
        memories = query.all()

        if len(memories) < 2:
            return []

        # Group into batches
        results = []
        for i in range(0, len(memories), batch_size):
            batch = memories[i : i + batch_size]
            if len(batch) < 2:
                continue

            memory_ids = [m.memory_id for m in batch]

            try:
                result = _run_coroutine(self.consolidate_async(memory_ids, importance_max))
                results.append(result)
            except Exception as e:
                # Log error but continue with other batches
                logger.warning("Consolidation batch failed: %s", e)
                continue

        return results

    def _load_memory(self, memory_id: str) -> dict[str, Any] | None:
        """Load memory with content.

        Args:
            memory_id: Memory ID

        Returns:
            Memory data dictionary or None if not found
        """
        query = self.session.query(MemoryModel).filter_by(memory_id=memory_id)
        if self.user_id:
            query = query.filter(MemoryModel.user_id == self.user_id)
        if self.zone_id:
            query = query.filter(MemoryModel.zone_id == self.zone_id)
        memory = query.first()
        if not memory:
            return None

        try:
            content_bytes = self.backend.read_content(memory.content_hash).unwrap()
            content = content_bytes.decode("utf-8")
        except Exception:
            content = ""

        return {
            "memory_id": memory.memory_id,
            "content": content,
            "memory_type": memory.memory_type,
            "importance": memory.importance or 0.0,
            "scope": memory.scope,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
        }

    def _build_consolidation_prompt(self, memories: list[dict[str, Any]]) -> str:
        """Build consolidation prompt for LLM.

        Args:
            memories: List of memory dictionaries

        Returns:
            Consolidation prompt
        """
        prompt = """# Memory Consolidation Task

You are consolidating multiple related memories into a concise, high-value summary.

## Source Memories

"""

        for i, memory in enumerate(memories, 1):
            content = memory.get("content", "")
            importance = memory.get("importance", 0.0)
            mem_type = memory.get("memory_type", "unknown")

            prompt += f"""### Memory {i} (Type: {mem_type}, Importance: {importance:.2f})
{content}

"""

        prompt += """## Your Task

Create a consolidated summary that:
1. Captures the essential information from all source memories
2. Removes redundancy while preserving unique insights
3. Maintains factual accuracy
4. Is concise yet comprehensive

Provide only the consolidated summary, no additional commentary.
"""

        return prompt

    def _store_consolidated_memory(
        self,
        source_memories: list[dict[str, Any]],
        consolidated_content: str,
        importance: float,
    ) -> str:
        """Store consolidated memory.

        Args:
            source_memories: List of source memory dictionaries
            consolidated_content: Consolidated content text
            importance: Importance score

        Returns:
            memory_id: ID of consolidated memory
        """
        memory_id = str(uuid.uuid4())

        # Prepare consolidated content with metadata
        content_data = {
            "type": "consolidated",
            "content": consolidated_content,
            "source_count": len(source_memories),
            "consolidated_at": datetime.now(UTC).isoformat(),
        }

        # Store in CAS
        content_json = json.dumps(content_data, indent=2).encode("utf-8")
        content_hash = self.backend.write_content(content_json).unwrap()

        # Track source memory IDs
        source_ids = [m["memory_id"] for m in source_memories]

        # Create memory record
        memory = MemoryModel(
            memory_id=memory_id,
            content_hash=content_hash,
            zone_id=self.zone_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            scope="agent",
            visibility="private",
            memory_type="consolidated",
            importance=importance,
            consolidated_from=json.dumps(source_ids),
            consolidation_version=1,
        )

        self.session.add(memory)
        self.session.commit()

        return memory_id

    def _mark_memories_consolidated(
        self,
        memory_ids: list[str],
        consolidated_memory_id: str,
    ) -> None:
        """Mark source memories as consolidated, archived, and linked.

        Archives source memories and lowers their importance so they don't
        appear alongside the consolidated result in search. Links each source
        to the consolidated parent via parent_memory_id.

        Args:
            memory_ids: List of source memory IDs.
            consolidated_memory_id: ID of the new consolidated memory.
        """
        from sqlalchemy import update

        if not memory_ids:
            return

        stmt = update(MemoryModel).where(MemoryModel.memory_id.in_(memory_ids))
        if self.user_id:
            stmt = stmt.where(MemoryModel.user_id == self.user_id)

        stmt = stmt.values(
            parent_memory_id=consolidated_memory_id,
            is_archived=True,
            importance=0.1,
        )
        self.session.execute(stmt)
        self.session.commit()

    def sync_consolidate(
        self,
        memory_ids: list[str],
        importance_threshold: float = 0.5,
        max_consolidated_memories: int = 10,
    ) -> dict[str, Any]:
        """Synchronous wrapper for consolidate_async.

        Safe to call from both sync and async contexts. When a running
        event loop is detected, spins up a fresh session in a new thread
        to avoid sharing the SQLAlchemy session across threads.

        Args:
            memory_ids: List of memory IDs to consolidate
            importance_threshold: Only consolidate memories below this importance
            max_consolidated_memories: Maximum memories to include

        Returns:
            Consolidation results
        """
        return _run_coroutine(
            self.consolidate_async(memory_ids, importance_threshold, max_consolidated_memories)
        )

    # =========================================================================
    # Affinity-based consolidation (Issue #1026 - SimpleMem-inspired)
    # =========================================================================

    async def consolidate_by_affinity_async(
        self,
        memory_ids: list[str] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        beta: float = 0.7,
        lambda_decay: float = 0.1,
        affinity_threshold: float = 0.85,
        time_unit_hours: float = 24.0,
        max_cluster_size: int = 20,
        importance_max: float = 0.5,
        memory_type: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Consolidate memories using affinity-based clustering.

        This method implements SimpleMem-inspired affinity scoring that combines
        semantic similarity and temporal proximity for smarter memory clustering.

        The affinity formula:
            affinity = beta * cos(v_i, v_j) + (1 - beta) * exp(-lambda * |t_i - t_j|)

        Args:
            memory_ids: Optional list of specific memory IDs to consider.
                If not provided, queries candidate memories automatically.
            embedding_provider: Provider for generating embeddings. If not provided,
                uses OpenAI text-embedding-3-small by default.
            beta: Semantic similarity weight (0-1). Default 0.7 (semantic-dominant).
            lambda_decay: Temporal decay rate. Default 0.1.
            affinity_threshold: Minimum affinity for clustering. Default 0.85.
            time_unit_hours: Time normalization factor in hours. Default 24.0 (1 day).
            max_cluster_size: Maximum memories per cluster. Default 20.
            importance_max: Only consider memories with importance <= this. Default 0.5.
            memory_type: Optional filter by memory type.
            namespace: Optional filter by namespace.
            limit: Maximum total memories to process. Default 100.

        Returns:
            Dictionary with consolidation results:
                - clusters_formed: Number of clusters created
                - total_consolidated: Total memories consolidated
                - results: List of consolidation results per cluster
                - cluster_statistics: Statistics for each cluster

        Example:
            >>> result = await engine.consolidate_by_affinity_async(
            ...     beta=0.7,
            ...     affinity_threshold=0.85,
            ...     importance_max=0.5,
            ... )
            >>> print(f"Formed {result['clusters_formed']} clusters")

        Reference:
            SimpleMem: Efficient Lifelong Memory for LLM Agents
            https://arxiv.org/html/2601.02553
        """
        # Build affinity config
        affinity_config = AffinityConfig(
            beta=beta,
            lambda_decay=lambda_decay,
            time_unit_hours=time_unit_hours,
            cluster_threshold=affinity_threshold,
            linkage="average",
            min_cluster_size=2,
        )

        # Step 1: Load candidate memories
        if memory_ids:
            memory_vectors = await self._load_memory_vectors(memory_ids)
        else:
            memory_vectors = await self._query_candidate_memories(
                importance_max=importance_max,
                memory_type=memory_type,
                namespace=namespace,
                limit=limit,
            )

        if len(memory_vectors) < 2:
            logger.info("Not enough memories for affinity clustering (need >= 2)")
            return {
                "clusters_formed": 0,
                "total_consolidated": 0,
                "results": [],
                "cluster_statistics": [],
            }

        # Step 2: Ensure all memories have embeddings
        memory_vectors, embedding_warnings = await self._ensure_embeddings(
            memory_vectors, embedding_provider
        )

        # Filter out memories without embeddings
        memory_vectors = [m for m in memory_vectors if m.embedding]
        if len(memory_vectors) < 2:
            logger.warning("Not enough memories with embeddings for clustering")
            result: dict[str, Any] = {
                "clusters_formed": 0,
                "total_consolidated": 0,
                "results": [],
                "cluster_statistics": [],
            }
            if embedding_warnings:
                result["warnings"] = embedding_warnings
            return result

        # Step 3: Cluster by affinity
        try:
            cluster_result = cluster_by_affinity(memory_vectors, affinity_config)
        except Exception as e:
            logger.error("Clustering failed: %s", e)
            return {
                "clusters_formed": 0,
                "total_consolidated": 0,
                "results": [],
                "cluster_statistics": [],
                "error": str(e),
            }

        # Step 4: Get cluster statistics
        cluster_stats = get_cluster_statistics(memory_vectors, cluster_result, affinity_config)

        # Step 5: Consolidate each cluster concurrently (bounded)
        import asyncio

        semaphore = asyncio.Semaphore(3)  # Max 3 concurrent LLM calls

        async def _consolidate_cluster(cluster_ids: list[str]) -> dict[str, Any] | None:
            cluster_ids_limited = cluster_ids[:max_cluster_size]
            async with semaphore:
                try:
                    return await self.consolidate_async(
                        memory_ids=cluster_ids_limited,
                        importance_threshold=importance_max + 0.1,
                        max_consolidated_memories=max_cluster_size,
                    )
                except Exception as e:
                    logger.warning("Cluster consolidation failed: %s", e)
                    return None

        cluster_results = await asyncio.gather(
            *[_consolidate_cluster(cids) for cids in cluster_result.clusters]
        )

        results = [r for r in cluster_results if r is not None]
        total_consolidated = sum(r.get("memories_consolidated", 0) for r in results)

        logger.info(
            "Affinity consolidation complete: %d clusters, %d memories consolidated",
            len(results),
            total_consolidated,
        )

        final: dict[str, Any] = {
            "clusters_formed": len(results),
            "total_consolidated": total_consolidated,
            "archived_count": total_consolidated,
            "results": results,
            "cluster_statistics": cluster_stats,
        }
        if embedding_warnings:
            final["warnings"] = embedding_warnings
        return final

    async def _load_memory_vectors(
        self,
        memory_ids: list[str],
    ) -> list[MemoryVector]:
        """Load memories as MemoryVector objects for clustering.

        Uses a single batch query (IN clause) instead of N individual queries.
        Filters by user_id and zone_id for ownership enforcement.

        Args:
            memory_ids: List of memory IDs to load.

        Returns:
            List of MemoryVector objects with content and embeddings.
        """
        if not memory_ids:
            return []

        # Batch query with ownership filter
        query = self.session.query(MemoryModel).filter(
            MemoryModel.memory_id.in_(memory_ids),
        )
        if self.user_id:
            query = query.filter(MemoryModel.user_id == self.user_id)
        if self.zone_id:
            query = query.filter(MemoryModel.zone_id == self.zone_id)

        memories = query.all()

        return self._models_to_vectors(memories)

    async def _query_candidate_memories(
        self,
        importance_max: float = 0.5,
        memory_type: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[MemoryVector]:
        """Query candidate memories for consolidation.

        Builds MemoryVector objects directly from query results (no second
        round-trip). Filters by agent_id, user_id, and zone_id for ownership.

        Args:
            importance_max: Maximum importance score.
            memory_type: Optional filter by type.
            namespace: Optional filter by namespace.
            limit: Maximum memories to return.

        Returns:
            List of MemoryVector objects.
        """
        query = self.session.query(MemoryModel).filter(
            MemoryModel.agent_id == self.agent_id,
            MemoryModel.importance <= importance_max,
            MemoryModel.consolidated_from.is_(None),  # Not already consolidated
            MemoryModel.is_archived == False,  # noqa: E712 — skip archived
        )

        if self.user_id:
            query = query.filter(MemoryModel.user_id == self.user_id)
        if self.zone_id:
            query = query.filter(MemoryModel.zone_id == self.zone_id)
        if memory_type:
            query = query.filter_by(memory_type=memory_type)
        if namespace:
            query = query.filter_by(namespace=namespace)

        query = query.order_by(MemoryModel.created_at.desc()).limit(limit)
        memories = query.all()

        # Build MemoryVectors directly — no second DB round-trip
        return self._models_to_vectors(memories)

    def _models_to_vectors(self, memories: list[MemoryModel]) -> list[MemoryVector]:
        """Convert MemoryModel objects to MemoryVector for clustering.

        Loads content from CAS and parses embeddings from JSON.

        Args:
            memories: List of MemoryModel objects.

        Returns:
            List of MemoryVector objects with content and embeddings.
        """
        vectors = []
        for memory in memories:
            # Load content from CAS
            try:
                content_bytes = self.backend.read_content(memory.content_hash).unwrap()
                content = content_bytes.decode("utf-8")
            except Exception:
                content = ""

            # Parse embedding if available
            embedding: list[float] = []
            if memory.embedding:
                try:
                    parsed = json.loads(memory.embedding)
                    if isinstance(parsed, list):
                        embedding = parsed
                except (json.JSONDecodeError, TypeError):
                    pass

            vectors.append(
                MemoryVector(
                    memory_id=memory.memory_id,
                    embedding=embedding,
                    created_at=memory.created_at or datetime.now(UTC),
                    content=content,
                    importance=memory.importance,
                    memory_type=memory.memory_type,
                )
            )
        return vectors

    async def _ensure_embeddings(
        self,
        memory_vectors: list[MemoryVector],
        embedding_provider: EmbeddingProvider | None = None,
    ) -> tuple[list[MemoryVector], list[str]]:
        """Ensure all memory vectors have embeddings.

        Returns a new list — does not mutate the input. Memories that already
        have embeddings are passed through; memories without embeddings get
        new MemoryVector instances with the generated embedding.

        Also persists new embeddings to the database.

        Args:
            memory_vectors: List of MemoryVector objects (not mutated).
            embedding_provider: Provider for generating embeddings.

        Returns:
            Tuple of (vectors with embeddings filled in, list of warning messages).
        """
        warnings: list[str] = []

        # Split into has-embedding and needs-embedding
        needs_embedding_indices = [i for i, m in enumerate(memory_vectors) if not m.embedding]

        if not needs_embedding_indices:
            return list(memory_vectors), warnings

        # Create embedding provider if not provided
        if embedding_provider is None:
            try:
                from nexus.search.embeddings import create_embedding_provider

                embedding_provider = create_embedding_provider("openai", "text-embedding-3-small")
            except Exception as e:
                msg = f"Could not create embedding provider: {e}"
                logger.warning("Could not create embedding provider: %s", e)
                warnings.append(msg)
                return list(memory_vectors), warnings

        # Build result list (copy to avoid mutation)
        result = list(memory_vectors)

        # Generate embeddings in batch
        try:
            texts = [memory_vectors[i].content or "" for i in needs_embedding_indices]
            embeddings = await embedding_provider.embed_texts_batched(texts)

            # Create new MemoryVector instances with embeddings (immutable)
            for batch_idx, vec_idx in enumerate(needs_embedding_indices):
                old = memory_vectors[vec_idx]
                result[vec_idx] = MemoryVector(
                    memory_id=old.memory_id,
                    embedding=embeddings[batch_idx],
                    created_at=old.created_at,
                    content=old.content,
                    importance=old.importance,
                    memory_type=old.memory_type,
                )

                # Persist to database
                memory = self.session.query(MemoryModel).filter_by(memory_id=old.memory_id).first()
                if memory:
                    memory.embedding = json.dumps(embeddings[batch_idx])
                    memory.embedding_model = getattr(embedding_provider, "model", "unknown")
                    memory.embedding_dim = len(embeddings[batch_idx])

            self.session.commit()
            logger.info("Generated embeddings for %d memories", len(needs_embedding_indices))

        except Exception as e:
            msg = f"Failed to generate embeddings: {e}"
            logger.error("Failed to generate embeddings: %s", e)
            warnings.append(msg)

        return result, warnings

    def sync_consolidate_by_affinity(
        self,
        memory_ids: list[str] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        beta: float = 0.7,
        lambda_decay: float = 0.1,
        affinity_threshold: float = 0.85,
        time_unit_hours: float = 24.0,
        max_cluster_size: int = 20,
        importance_max: float = 0.5,
    ) -> dict[str, Any]:
        """Synchronous wrapper for consolidate_by_affinity_async.

        Safe to call from both sync and async contexts. When a running
        event loop is detected, spins up a fresh session in a new thread.

        Args:
            memory_ids: Optional list of specific memory IDs to consider.
            embedding_provider: Provider for generating embeddings.
            beta: Semantic similarity weight (0-1). Default 0.7.
            lambda_decay: Temporal decay rate. Default 0.1.
            affinity_threshold: Minimum affinity for clustering. Default 0.85.
            time_unit_hours: Time normalization factor in hours. Default 24.0.
            max_cluster_size: Maximum memories per cluster. Default 20.
            importance_max: Only consider memories with importance <= this. Default 0.5.

        Returns:
            Consolidation results (same as consolidate_by_affinity_async).
        """
        return _run_coroutine(
            self.consolidate_by_affinity_async(
                memory_ids=memory_ids,
                embedding_provider=embedding_provider,
                beta=beta,
                lambda_decay=lambda_decay,
                affinity_threshold=affinity_threshold,
                time_unit_hours=time_unit_hours,
                max_cluster_size=max_cluster_size,
                importance_max=importance_max,
            )
        )
