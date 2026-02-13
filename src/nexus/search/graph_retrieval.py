"""Graph-enhanced retrieval with dual-level search (LightRAG style).

Implements LightRAG-style dual-level retrieval combining:
- Low-level (entity-based): Entity matching + N-hop neighbor expansion
- High-level (theme-based): Theme/cluster context from hierarchical memory

This module provides graph-aware scoring and context enrichment on top of
existing semantic/hybrid search capabilities.

Issue #1040: Graph-enhanced retrieval with dual-level search (LightRAG)

References:
    - LightRAG Paper: https://arxiv.org/abs/2410.05779 (Dual-level retrieval)
    - LightRAG GitHub: https://github.com/HKUDS/LightRAG
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nexus.search.fusion import normalize_scores_minmax

if TYPE_CHECKING:
    from nexus.services.ace.memory_hierarchy import HierarchicalMemoryManager
    from nexus.search.embeddings import EmbeddingProvider
    from nexus.search.graph_store import Entity, GraphStore, Relationship
    from nexus.search.semantic import SemanticSearch, SemanticSearchResult

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================


VALID_GRAPH_MODES = {"none", "low", "high", "dual"}


@dataclass
class GraphRetrievalConfig:
    """Configuration for graph-enhanced retrieval.

    Attributes:
        graph_mode: Graph enhancement mode
            - "none": Traditional search only (backward compatible)
            - "low": Entity matching + N-hop expansion
            - "high": Theme/cluster context from hierarchical memory
            - "dual": Full LightRAG-style dual-level search
        entity_similarity_threshold: Minimum similarity for entity matching (0.0-1.0)
        neighbor_hops: N-hop expansion depth for entity neighbors
        max_entities_per_query: Maximum entities to extract from query
        prefer_abstracts: Prefer high-level abstractions in theme search
        expand_threshold: Score threshold to expand theme to children
        max_children_per_abstract: Maximum children per abstract theme
        lambda_semantic: Weight for semantic (vector) score
        lambda_keyword: Weight for keyword (BM25) score
        lambda_graph: Weight for graph proximity score
        parallel_search: Run low/high level searches in parallel
    """

    graph_mode: str = "none"
    # Low-level (entity) settings
    entity_similarity_threshold: float = 0.75
    neighbor_hops: int = 2
    max_entities_per_query: int = 5
    # High-level (theme) settings
    prefer_abstracts: bool = True
    expand_threshold: float = 0.7
    max_children_per_abstract: int = 2
    # Fusion weights (should sum to ~1.0)
    lambda_semantic: float = 0.4
    lambda_keyword: float = 0.3
    lambda_graph: float = 0.3
    # Performance
    parallel_search: bool = True

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.graph_mode not in VALID_GRAPH_MODES:
            raise ValueError(
                f"Invalid graph_mode '{self.graph_mode}'. Must be one of: {VALID_GRAPH_MODES}"
            )
        if not 0.0 <= self.entity_similarity_threshold <= 1.0:
            raise ValueError("entity_similarity_threshold must be between 0.0 and 1.0")
        if self.neighbor_hops < 0 or self.neighbor_hops > 10:
            raise ValueError("neighbor_hops must be between 0 and 10")


# =============================================================================
# Result Data Classes
# =============================================================================


@dataclass
class GraphContext:
    """Graph context enrichment for a search result.

    Contains entity and relationship information extracted from the knowledge
    graph for a given search result chunk.
    """

    # Entities found in or related to this result
    entities: list[Entity] = field(default_factory=list)
    # Relationships connecting entities
    relationships: list[Relationship] = field(default_factory=list)
    # Theme/cluster information (for high-level context)
    theme: str | None = None
    theme_abstraction_level: int = 0
    parent_theme_id: str | None = None
    # Scoring
    graph_proximity_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "entities": [e.to_dict() for e in self.entities] if self.entities else [],
            "relationships": [
                {
                    "source": r.source_entity_id,
                    "predicate": r.relationship_type,
                    "target": r.target_entity_id,
                    "confidence": r.confidence,
                }
                for r in self.relationships
            ]
            if self.relationships
            else [],
            "theme": self.theme,
            "theme_abstraction_level": self.theme_abstraction_level,
            "graph_proximity_score": self.graph_proximity_score,
        }


@dataclass
class GraphEnhancedSearchResult:
    """Search result with graph context enrichment.

    Extends the base SemanticSearchResult with graph-aware scoring and
    entity/relationship context.
    """

    # Base result fields (from SemanticSearchResult)
    path: str
    chunk_index: int
    chunk_text: str
    score: float
    start_offset: int | None = None
    end_offset: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    # Individual score components
    keyword_score: float | None = None
    vector_score: float | None = None
    graph_score: float | None = None  # Graph proximity score
    # Graph context enrichment
    graph_context: GraphContext | None = None
    # Chunk ID for entity linkage
    chunk_id: str | None = None

    @classmethod
    def from_semantic_result(
        cls,
        result: SemanticSearchResult,
        chunk_id: str | None = None,
    ) -> GraphEnhancedSearchResult:
        """Create from a SemanticSearchResult."""
        return cls(
            path=result.path,
            chunk_index=result.chunk_index,
            chunk_text=result.chunk_text,
            score=result.score,
            start_offset=result.start_offset,
            end_offset=result.end_offset,
            line_start=result.line_start,
            line_end=result.line_end,
            keyword_score=result.keyword_score,
            vector_score=result.vector_score,
            chunk_id=chunk_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "path": self.path,
            "chunk_index": self.chunk_index,
            "chunk_text": self.chunk_text,
            "score": self.score,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "keyword_score": self.keyword_score,
            "vector_score": self.vector_score,
            "graph_score": self.graph_score,
            "graph_context": self.graph_context.to_dict() if self.graph_context else None,
        }


# =============================================================================
# Graph-Enhanced Fusion
# =============================================================================


def graph_enhanced_fusion(
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    graph_boost_ids: set[str],
    theme_boost_ids: set[str],
    lambda_semantic: float = 0.4,
    lambda_keyword: float = 0.3,
    lambda_graph: float = 0.3,
    limit: int = 10,
    id_key: str = "chunk_id",
) -> list[dict[str, Any]]:
    """Fuse results with graph proximity boosting.

    Combines keyword, vector, and graph signals:
        Score = λ₁·semantic + λ₂·BM25 + λ₃·graph_proximity

    Where graph_proximity is:
        - 1.0 if chunk_id in graph_boost_ids (direct entity match)
        - 0.7 if chunk_id in theme_boost_ids (theme context)
        - 0.0 otherwise

    Args:
        keyword_results: Results from BM25/keyword search
        vector_results: Results from semantic/vector search
        graph_boost_ids: Chunk IDs from entity/neighbor expansion
        theme_boost_ids: Chunk IDs from theme/hierarchy matching
        lambda_semantic: Weight for semantic score (default: 0.4)
        lambda_keyword: Weight for keyword score (default: 0.3)
        lambda_graph: Weight for graph proximity score (default: 0.3)
        limit: Maximum results to return
        id_key: Key for result identification (default: "chunk_id")

    Returns:
        Fused results with graph_score populated, sorted by combined score
    """
    # Merge all results into a single map
    results_map: dict[str, dict[str, Any]] = {}

    def get_key(result: dict[str, Any]) -> str:
        if id_key and id_key in result:
            return str(result[id_key])
        return f"{result.get('path', '')}:{result.get('chunk_index', 0)}"

    # Normalize keyword scores
    keyword_scores = [r.get("score", 0.0) for r in keyword_results]
    norm_keyword = normalize_scores_minmax(keyword_scores) if keyword_scores else []

    # Normalize vector scores
    vector_scores = [r.get("score", 0.0) for r in vector_results]
    norm_vector = normalize_scores_minmax(vector_scores) if vector_scores else []

    # Add keyword results
    for i, result in enumerate(keyword_results):
        key = get_key(result)
        if key not in results_map:
            results_map[key] = result.copy()
            results_map[key]["_keyword_norm"] = 0.0
            results_map[key]["_vector_norm"] = 0.0
        results_map[key]["keyword_score"] = result.get("score", 0.0)
        results_map[key]["_keyword_norm"] = norm_keyword[i] if i < len(norm_keyword) else 0.0

    # Add vector results
    for i, result in enumerate(vector_results):
        key = get_key(result)
        if key not in results_map:
            results_map[key] = result.copy()
            results_map[key]["_keyword_norm"] = 0.0
            results_map[key]["_vector_norm"] = 0.0
        results_map[key]["vector_score"] = result.get("score", 0.0)
        results_map[key]["_vector_norm"] = norm_vector[i] if i < len(norm_vector) else 0.0

    # Calculate graph proximity and combined scores
    for key, result in results_map.items():
        # Graph proximity score
        chunk_id = result.get("chunk_id", key)
        if chunk_id in graph_boost_ids:
            graph_score = 1.0  # Direct entity match
        elif chunk_id in theme_boost_ids:
            graph_score = 0.7  # Theme context
        else:
            graph_score = 0.0

        result["graph_score"] = graph_score

        # Combined score
        result["score"] = (
            lambda_semantic * result["_vector_norm"]
            + lambda_keyword * result["_keyword_norm"]
            + lambda_graph * graph_score
        )

        # Clean up internal fields
        result.pop("_keyword_norm", None)
        result.pop("_vector_norm", None)

    # Sort by combined score and return top results
    sorted_results = sorted(
        results_map.values(),
        key=lambda x: x["score"],
        reverse=True,
    )

    return sorted_results[:limit]


# =============================================================================
# Main Orchestrator Class
# =============================================================================


class GraphEnhancedRetriever:
    """Orchestrates graph-enhanced retrieval with dual-level search.

    Combines:
    1. Traditional search (keyword + semantic) via SemanticSearch
    2. Low-level graph search (entity + neighbor expansion) via GraphStore
    3. High-level graph search (theme + hierarchy) via HierarchicalMemoryManager

    Following LightRAG best practices:
    - Entity matching via embedding similarity
    - N-hop neighbor expansion for context
    - Theme-based retrieval from hierarchical memory
    - 3-way score fusion with configurable weights

    Example:
        >>> from nexus.search import SemanticSearch, GraphEnhancedRetriever
        >>> from nexus.search.graph_store import GraphStore
        >>> retriever = GraphEnhancedRetriever(
        ...     semantic_search=semantic_search,
        ...     graph_store=graph_store,
        ...     config=GraphRetrievalConfig(graph_mode="dual"),
        ... )
        >>> results = await retriever.search("authentication system", limit=10)
        >>> for r in results:
        ...     print(f"{r.path}: {r.score:.3f}")
        ...     if r.graph_context:
        ...         print(f"  Entities: {[e.canonical_name for e in r.graph_context.entities]}")
    """

    def __init__(
        self,
        semantic_search: SemanticSearch,
        graph_store: GraphStore | None = None,
        hierarchy_manager: HierarchicalMemoryManager | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        config: GraphRetrievalConfig | None = None,
    ):
        """Initialize graph-enhanced retriever.

        Args:
            semantic_search: SemanticSearch instance for keyword+vector search
            graph_store: GraphStore instance for entity operations (optional)
            hierarchy_manager: HierarchicalMemoryManager for theme search (optional)
            embedding_provider: EmbeddingProvider for query embeddings (optional,
                               falls back to semantic_search.embedding_provider)
            config: GraphRetrievalConfig for behavior configuration
        """
        self.semantic_search = semantic_search
        self.graph_store = graph_store
        self.hierarchy_manager = hierarchy_manager
        self.embedding_provider = embedding_provider or getattr(
            semantic_search, "embedding_provider", None
        )
        self.config = config or GraphRetrievalConfig()

        # Validate dependencies based on mode
        if self.config.graph_mode in ("low", "dual") and not self.graph_store:
            logger.warning(
                "[GRAPH-RETRIEVAL] graph_mode='%s' requires graph_store, falling back to 'none'",
                self.config.graph_mode,
            )
            self.config.graph_mode = "none"

        if self.config.graph_mode in ("high", "dual") and not self.hierarchy_manager:
            logger.warning(
                "[GRAPH-RETRIEVAL] graph_mode='%s' requires hierarchy_manager, "
                "falling back to '%s'",
                self.config.graph_mode,
                "low" if self.graph_store else "none",
            )
            self.config.graph_mode = "low" if self.graph_store else "none"

    async def search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        graph_mode: str | None = None,
        search_mode: str = "hybrid",
        alpha: float = 0.5,
        include_graph_context: bool = True,
    ) -> list[GraphEnhancedSearchResult]:
        """Execute graph-enhanced search.

        Args:
            query: Natural language query string
            path: Root path to search (default: "/" for all files)
            limit: Maximum results to return
            graph_mode: Override config graph_mode ("none", "low", "high", "dual")
            search_mode: Base search mode ("keyword", "semantic", "hybrid")
            alpha: Weight for vector search in hybrid mode
            include_graph_context: Whether to populate graph_context in results

        Returns:
            List of GraphEnhancedSearchResult with graph context enrichment

        Raises:
            ValueError: If graph_mode is invalid
        """
        mode = graph_mode or self.config.graph_mode

        if mode not in VALID_GRAPH_MODES:
            raise ValueError(f"Invalid graph_mode '{mode}'. Must be one of: {VALID_GRAPH_MODES}")

        logger.info(
            "[GRAPH-RETRIEVAL] Query: '%s', mode: %s, limit: %d",
            query[:50],
            mode,
            limit,
        )

        # Mode: none - just use traditional search
        if mode == "none":
            base_results = await self.semantic_search.search(
                query=query,
                path=path,
                limit=limit,
                search_mode=search_mode,
                alpha=alpha,
            )
            return [GraphEnhancedSearchResult.from_semantic_result(r) for r in base_results]

        # Generate query embedding (needed for entity/theme matching)
        query_embedding = None
        if self.embedding_provider:
            embeddings = await self.embedding_provider.embed_texts([query])
            query_embedding = embeddings[0] if embeddings else None

        # Execute searches based on mode
        graph_boost_ids: set[str] = set()
        theme_boost_ids: set[str] = set()
        matched_entities: list[Entity] = []

        if mode in ("low", "dual") and query_embedding and self.graph_store:
            entities, chunk_ids = await self._search_low_level(
                query=query,
                query_embedding=query_embedding,
                limit=limit,
            )
            matched_entities = entities
            graph_boost_ids = chunk_ids

        if mode in ("high", "dual") and query_embedding and self.hierarchy_manager:
            theme_chunk_ids = await self._search_high_level(
                query_embedding=query_embedding,
                limit=limit,
            )
            theme_boost_ids = theme_chunk_ids

        # Get base search results (keyword + semantic)
        base_results = await self.semantic_search.search(
            query=query,
            path=path,
            limit=limit * 2,  # Get more for fusion
            search_mode=search_mode,
            alpha=alpha,
        )

        # Convert to dicts for fusion
        keyword_results = []
        vector_results = []
        for r in base_results:
            result_dict = {
                "path": r.path,
                "chunk_index": r.chunk_index,
                "chunk_text": r.chunk_text,
                "score": r.score,
                "start_offset": r.start_offset,
                "end_offset": r.end_offset,
                "line_start": r.line_start,
                "line_end": r.line_end,
                "chunk_id": f"{r.path}:{r.chunk_index}",
            }
            if r.keyword_score is not None:
                keyword_results.append({**result_dict, "score": r.keyword_score})
            if r.vector_score is not None:
                vector_results.append({**result_dict, "score": r.vector_score})

        # Apply graph-enhanced fusion
        fused_results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=vector_results,
            graph_boost_ids=graph_boost_ids,
            theme_boost_ids=theme_boost_ids,
            lambda_semantic=self.config.lambda_semantic,
            lambda_keyword=self.config.lambda_keyword,
            lambda_graph=self.config.lambda_graph,
            limit=limit,
        )

        # Convert to GraphEnhancedSearchResult
        results = [
            GraphEnhancedSearchResult(
                path=r["path"],
                chunk_index=r["chunk_index"],
                chunk_text=r["chunk_text"],
                score=r["score"],
                start_offset=r.get("start_offset"),
                end_offset=r.get("end_offset"),
                line_start=r.get("line_start"),
                line_end=r.get("line_end"),
                keyword_score=r.get("keyword_score"),
                vector_score=r.get("vector_score"),
                graph_score=r.get("graph_score"),
                chunk_id=r.get("chunk_id"),
            )
            for r in fused_results
        ]

        # Enrich with graph context if requested
        if include_graph_context and self.graph_store:
            await self._enrich_with_graph_context(results, matched_entities)

        logger.info(
            "[GRAPH-RETRIEVAL] Returned %d results, graph_boosted=%d, theme_boosted=%d",
            len(results),
            len(graph_boost_ids),
            len(theme_boost_ids),
        )

        return results

    async def _search_low_level(
        self,
        query: str,  # noqa: ARG002 - kept for logging/future use
        query_embedding: list[float],
        limit: int,  # noqa: ARG002 - used indirectly via config
    ) -> tuple[list[Entity], set[str]]:
        """Low-level entity search with N-hop expansion.

        1. Find entities similar to query embedding
        2. Expand to N-hop neighbors
        3. Collect chunk_ids where entities are mentioned

        Args:
            query: Original query text (for logging)
            query_embedding: Query vector for entity matching
            limit: Maximum entities to match

        Returns:
            Tuple of (matched_entities, related_chunk_ids)
        """
        if not self.graph_store:
            return [], set()

        matched_entities: list[Entity] = []
        chunk_ids: set[str] = set()

        try:
            # Find entities similar to query
            similar_entities = await self.graph_store.find_similar_entities(
                embedding=query_embedding,
                threshold=self.config.entity_similarity_threshold,
                limit=self.config.max_entities_per_query,
            )

            for entity, _similarity in similar_entities:
                matched_entities.append(entity)

                # Get chunks where this entity is mentioned
                mentions = await self.graph_store.get_entity_mentions(
                    entity_id=entity.entity_id,
                    limit=50,
                )
                for mention in mentions:
                    if mention.chunk_id:
                        chunk_ids.add(mention.chunk_id)

                # Expand to neighbors
                if self.config.neighbor_hops > 0:
                    neighbors = await self.graph_store.get_neighbors(
                        entity_id=entity.entity_id,
                        hops=self.config.neighbor_hops,
                        direction="both",
                    )
                    for neighbor in neighbors:
                        neighbor_mentions = await self.graph_store.get_entity_mentions(
                            entity_id=neighbor.entity.entity_id,
                            limit=20,
                        )
                        for mention in neighbor_mentions:
                            if mention.chunk_id:
                                chunk_ids.add(mention.chunk_id)

            logger.debug(
                "[GRAPH-RETRIEVAL] Low-level: %d entities matched, %d chunk_ids found",
                len(matched_entities),
                len(chunk_ids),
            )

        except Exception as e:
            logger.warning("[GRAPH-RETRIEVAL] Low-level search failed: %s", e)

        return matched_entities, chunk_ids

    async def _search_high_level(
        self,
        query_embedding: list[float],
        limit: int,
    ) -> set[str]:
        """High-level theme/abstract search.

        Uses HierarchicalMemoryManager to find relevant themes/abstracts
        and returns chunk_ids related to those themes.

        Args:
            query_embedding: Query vector for theme matching
            limit: Maximum themes to match

        Returns:
            Set of chunk_ids related to matching themes
        """
        if not self.hierarchy_manager:
            return set()

        chunk_ids: set[str] = set()

        try:
            result = self.hierarchy_manager.retrieve_with_hierarchy(
                query_embedding=query_embedding,
                max_results=limit,
                prefer_abstracts=self.config.prefer_abstracts,
                expand_threshold=self.config.expand_threshold,
                max_children_per_abstract=self.config.max_children_per_abstract,
            )

            # Extract chunk_ids from memories
            # Memories may have source_chunk_id or be linked via consolidated_from
            for memory in result.memories:
                # Try to get source chunk from memory metadata
                metadata_json = getattr(memory, "metadata_json", None)
                if metadata_json:
                    try:
                        metadata = json.loads(metadata_json)
                        if "source_chunk_id" in metadata:
                            chunk_ids.add(metadata["source_chunk_id"])
                        if "source_chunk_ids" in metadata:
                            chunk_ids.update(metadata["source_chunk_ids"])
                    except json.JSONDecodeError:
                        pass

                # Also use memory_id as potential chunk reference
                if memory.memory_id:
                    chunk_ids.add(f"memory:{memory.memory_id}")

            logger.debug(
                "[GRAPH-RETRIEVAL] High-level: %d themes found, abstracts=%d, atomics=%d",
                len(result.memories),
                result.abstracts_used,
                result.atomics_used,
            )

        except Exception as e:
            logger.warning("[GRAPH-RETRIEVAL] High-level search failed: %s", e)

        return chunk_ids

    async def _enrich_with_graph_context(
        self,
        results: list[GraphEnhancedSearchResult],
        matched_entities: list[Entity],
    ) -> None:
        """Populate graph_context for each result.

        Args:
            results: Results to enrich (modified in place)
            matched_entities: Entities matched from query
        """
        if not self.graph_store:
            return

        for result in results:
            try:
                # Find entities mentioned in this chunk
                chunk_id = result.chunk_id or f"{result.path}:{result.chunk_index}"
                entities = await self.graph_store.get_entities_in_chunk(chunk_id)

                # If no direct entities, use matched entities
                if not entities and matched_entities:
                    entities = matched_entities[:3]  # Limit to top 3

                if entities:
                    # Get subgraph for these entities
                    entity_ids = [e.entity_id for e in entities]
                    subgraph = await self.graph_store.get_subgraph(
                        entity_ids=entity_ids,
                        max_hops=1,
                    )

                    result.graph_context = GraphContext(
                        entities=subgraph.entities,
                        relationships=subgraph.relationships,
                        graph_proximity_score=result.graph_score or 0.0,
                    )
                else:
                    result.graph_context = GraphContext(
                        graph_proximity_score=result.graph_score or 0.0,
                    )

            except Exception as e:
                logger.debug(
                    "[GRAPH-RETRIEVAL] Failed to enrich chunk %s: %s",
                    result.chunk_id,
                    e,
                )
                result.graph_context = GraphContext(
                    graph_proximity_score=result.graph_score or 0.0,
                )
