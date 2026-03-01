"""Graph-enhanced search service (Issue #434, #1040).

Extracts graph-enhanced search business logic from the search router
into the service layer, per KERNEL-ARCHITECTURE.md requirement that
routers be thin adapters with no business logic.
"""

import logging
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class DaemonSemanticSearchWrapper:
    """Wraps search daemon as SemanticSearch interface for GraphEnhancedRetriever."""

    def __init__(self, daemon: Any, *, zone_id: str | None = None) -> None:
        self.daemon = daemon
        self.embedding_provider = getattr(daemon, "_embedding_provider", None)
        self._zone_id = zone_id

    async def search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        search_mode: str = "hybrid",
        alpha: float = 0.5,
        **kwargs: Any,  # noqa: ARG002
    ) -> list[Any]:
        from nexus.bricks.search.results import BaseSearchResult

        results = await self.daemon.search(
            query=query,
            search_type=search_mode,
            limit=limit,
            path_filter=path if path != "/" else None,
            alpha=alpha,
            zone_id=self._zone_id,
        )
        return [
            BaseSearchResult(
                path=r.path,
                chunk_index=r.chunk_index,
                chunk_text=r.chunk_text,
                score=r.score,
                start_offset=r.start_offset,
                end_offset=r.end_offset,
                line_start=r.line_start,
                line_end=r.line_end,
                keyword_score=r.keyword_score,
                vector_score=r.vector_score,
            )
            for r in results
        ]


async def graph_enhanced_search(
    query: str,
    search_type: str,
    limit: int,
    path_filter: str | None,
    alpha: float,
    graph_mode: str,
    *,
    record_store: Any,
    async_session_factory: Any,
    search_daemon: Any,
    zone_id: str | None = None,
) -> list[Any]:
    """Execute graph-enhanced search using GraphEnhancedRetriever (Issue #1040).

    Creates a GraphEnhancedRetriever on-the-fly and executes the search.
    This function is called when graph_mode is not "none".

    Args:
        query: Search query text
        search_type: Base search type (keyword, semantic, hybrid)
        limit: Maximum results
        path_filter: Optional path prefix filter
        alpha: Semantic vs keyword weight
        graph_mode: Graph enhancement mode (low, high, dual)
        record_store: RecordStoreABC instance (injected via app.state)
        async_session_factory: Async session factory from RecordStoreABC (injected via app.state)
        search_daemon: SearchDaemon instance (injected)

    Returns:
        List of GraphEnhancedSearchResult
    """
    from nexus.bricks.search.graph_retrieval import (
        GraphEnhancedRetriever,
        GraphRetrievalConfig,
    )
    from nexus.bricks.search.graph_store import GraphStore

    async with async_session_factory() as session:
        graph_store = GraphStore(record_store, session, zone_id=zone_id or ROOT_ZONE_ID)

        semantic_wrapper = DaemonSemanticSearchWrapper(search_daemon, zone_id=zone_id)
        embedding_provider = getattr(search_daemon, "_embedding_provider", None)

        config = GraphRetrievalConfig(
            graph_mode=graph_mode,
            entity_similarity_threshold=0.75,
            neighbor_hops=2,
        )

        retriever = GraphEnhancedRetriever(
            semantic_search=semantic_wrapper,
            graph_store=graph_store,
            embedding_provider=embedding_provider,
            config=config,
        )

        results: list[Any] = await retriever.search(
            query=query,
            path=path_filter or "/",
            limit=limit,
            graph_mode=graph_mode,
            search_mode=search_type,
            alpha=alpha,
            include_graph_context=True,
        )
        return results
