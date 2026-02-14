"""Search API router (Issue #951, #1040, #1041, #1174, #1288).

Provides search daemon endpoints:
- GET  /api/search/health   -- daemon health check (public, no auth)
- GET  /api/search/stats    -- daemon statistics
- GET  /api/search/query    -- execute search query (hybrid/semantic/keyword + graph modes)
- POST /api/search/refresh  -- notify daemon of file change
- POST /api/search/expand   -- LLM-based query expansion

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from nexus.server.api.v1.dependencies import (
    get_database_url,
    get_nexus_fs,
    get_optional_search_daemon,
    get_search_daemon,
)
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


# =============================================================================
# Helper: graph-enhanced search (Issue #1040)
# =============================================================================


async def _graph_enhanced_search(
    query: str,
    search_type: str,
    limit: int,
    path_filter: str | None,
    alpha: float,
    graph_mode: str,
    *,
    nexus_fs: Any,
    database_url: str | None,
    search_daemon: Any,
) -> list:
    """Execute graph-enhanced search using GraphEnhancedRetriever (Issue #1040).

    Creates a GraphEnhancedRetriever on-the-fly and executes the search.
    This helper is called when graph_mode is not "none".

    Args:
        query: Search query text
        search_type: Base search type (keyword, semantic, hybrid)
        limit: Maximum results
        path_filter: Optional path prefix filter
        alpha: Semantic vs keyword weight
        graph_mode: Graph enhancement mode (low, high, dual)
        nexus_fs: NexusFS instance (injected)
        database_url: Database URL string (injected)
        search_daemon: SearchDaemon instance (injected)

    Returns:
        List of GraphEnhancedSearchResult
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from nexus.search.graph_retrieval import (
        GraphEnhancedRetriever,
        GraphRetrievalConfig,
    )
    from nexus.search.graph_store import GraphStore
    from nexus.search.semantic import SemanticSearchResult

    # Get database URL
    db_url = database_url
    if not db_url:
        db_url = nexus_fs._record_store.database_url if nexus_fs._record_store else None

    # Convert to async URL
    if not db_url:
        raise RuntimeError("No database URL available for graph search endpoint")
    async_url = db_url
    if async_url.startswith("postgresql://"):
        async_url = async_url.replace("postgresql://", "postgresql+asyncpg://")
    elif async_url.startswith("sqlite:///"):
        async_url = async_url.replace("sqlite:///", "sqlite+aiosqlite:///")

    # Create async engine and session
    engine = create_async_engine(async_url, echo=False)
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session_factory() as session:
            # Initialize components
            graph_store = GraphStore(session, zone_id="default")

            # Create a wrapper for SemanticSearch that uses the search daemon
            class DaemonSemanticSearchWrapper:
                """Wraps search daemon as SemanticSearch interface."""

                def __init__(self, daemon: Any) -> None:
                    self.daemon = daemon
                    self.embedding_provider = getattr(daemon, "_embedding_provider", None)

                async def search(
                    self,
                    query: str,
                    path: str = "/",
                    limit: int = 10,
                    search_mode: str = "hybrid",
                    alpha: float = 0.5,
                ) -> list[SemanticSearchResult]:
                    # Map search_mode to daemon's search_type
                    results = await self.daemon.search(
                        query=query,
                        search_type=search_mode,
                        limit=limit,
                        path_filter=path if path != "/" else None,
                        alpha=alpha,
                    )
                    # Convert daemon results to SemanticSearchResult
                    return [
                        SemanticSearchResult(
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

            # Create wrapper and retriever
            semantic_wrapper = DaemonSemanticSearchWrapper(search_daemon)
            embedding_provider = getattr(search_daemon, "_embedding_provider", None)

            config = GraphRetrievalConfig(
                graph_mode=graph_mode,
                entity_similarity_threshold=0.75,
                neighbor_hops=2,
            )

            retriever = GraphEnhancedRetriever(
                semantic_search=semantic_wrapper,  # type: ignore
                graph_store=graph_store,
                embedding_provider=embedding_provider,
                config=config,
            )

            # Execute search
            results = await retriever.search(
                query=query,
                path=path_filter or "/",
                limit=limit,
                graph_mode=graph_mode,
                search_mode=search_type,
                alpha=alpha,
                include_graph_context=True,
            )

            return results
    finally:
        await engine.dispose()


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/api/search/health")
async def search_daemon_health(
    search_daemon: Any = Depends(get_optional_search_daemon),
) -> dict[str, Any]:
    """Health check for the search daemon.

    Returns daemon initialization status and component availability.
    """
    if not search_daemon:
        return {
            "status": "disabled",
            "daemon_enabled": False,
            "message": "Search daemon not enabled (set NEXUS_SEARCH_DAEMON=true)",
        }

    health: dict[str, Any] = search_daemon.get_health()
    return health


@router.get("/api/search/stats")
async def search_daemon_stats(
    search_daemon: Any = Depends(get_search_daemon),
) -> dict[str, Any]:
    """Get search daemon statistics.

    Returns performance metrics including latency, document counts, and component status.
    """
    stats: dict[str, Any] = search_daemon.get_stats()
    return stats


@router.get("/api/search/query")
async def search_query(
    q: str = Query(..., description="Search query text", min_length=1),
    type: str = Query("hybrid", description="Search type: keyword, semantic, or hybrid"),
    limit: int = Query(10, description="Maximum number of results", ge=1, le=100),
    path: str | None = Query(None, description="Optional path prefix filter"),
    alpha: float = Query(0.5, description="Semantic vs keyword weight (0.0-1.0)", ge=0.0, le=1.0),
    fusion: str = Query("rrf", description="Fusion method: rrf, weighted, or rrf_weighted"),
    adaptive_k: bool = Query(
        False,
        description="Adaptive retrieval: dynamically adjust limit based on query complexity",
    ),
    graph_mode: str = Query(
        "none",
        description="Graph enhancement mode (Issue #1040): none, low, high, dual, or auto",
    ),
    _auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(get_search_daemon),
    nexus_fs: Any = Depends(get_nexus_fs),
    database_url: str = Depends(get_database_url),
) -> dict[str, Any]:
    """Execute a fast search query using the search daemon.

    This endpoint uses pre-warmed indexes for sub-50ms response times.

    Args:
        q: Search query text
        type: Search type ("keyword", "semantic", or "hybrid")
        limit: Maximum number of results (1-100). Used as k_base when adaptive_k=True.
        path: Optional path prefix filter (e.g., "/docs/")
        alpha: Weight for semantic search (0.0 = all keyword, 1.0 = all semantic)
        fusion: Fusion method for hybrid search
        adaptive_k: If True, dynamically adjust limit based on query complexity (Issue #1021)
        graph_mode: Graph enhancement mode (Issue #1040):
            - "none": Traditional search only (default)
            - "low": Entity matching + N-hop neighbor expansion
            - "high": Theme/cluster context from hierarchical memory
            - "dual": Full LightRAG-style dual-level search
            - "auto": Automatically select based on query complexity (Issue #1041)

    Returns:
        Search results with scores and metadata
    """

    start_time = time.perf_counter()

    if not search_daemon.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Search daemon is still initializing",
        )

    # Validate search type
    if type not in ("keyword", "semantic", "hybrid"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid search type: {type}. Must be 'keyword', 'semantic', or 'hybrid'",
        )

    # Validate fusion method
    if fusion not in ("rrf", "weighted", "rrf_weighted"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid fusion method: {fusion}. Must be 'rrf', 'weighted', or 'rrf_weighted'",
        )

    # Validate graph mode (Issue #1040)
    if graph_mode not in ("none", "low", "high", "dual", "auto"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid graph_mode: {graph_mode}. Must be 'none', 'low', 'high', 'dual', or 'auto'",
        )

    # Query routing for auto mode (Issue #1041)
    routing_info: dict[str, Any] | None = None
    effective_graph_mode = graph_mode
    effective_limit = limit

    if graph_mode == "auto":
        from nexus.search.query_router import QueryRouter, RoutingConfig

        query_router = QueryRouter(config=RoutingConfig())
        routed = query_router.route(q, base_limit=limit)

        effective_graph_mode = routed.graph_mode
        effective_limit = routed.adjusted_limit
        routing_info = routed.to_dict()

        logger.info(
            f"[QUERY-ROUTER] {routed.reasoning}, "
            f"graph_mode={effective_graph_mode}, limit={effective_limit}"
        )

    try:
        # Use graph-enhanced search if effective_graph_mode is not "none" (Issue #1040)
        if effective_graph_mode != "none":
            results = await _graph_enhanced_search(
                query=q,
                search_type=type,
                limit=effective_limit,
                path_filter=path,
                alpha=alpha,
                graph_mode=effective_graph_mode,
                nexus_fs=nexus_fs,
                database_url=database_url,
                search_daemon=search_daemon,
            )
            latency_ms = (time.perf_counter() - start_time) * 1000

            response: dict[str, Any] = {
                "query": q,
                "search_type": type,
                "graph_mode": effective_graph_mode,
                "results": [
                    {
                        "path": r.path,
                        "chunk_text": r.chunk_text,
                        "score": round(r.score, 4),
                        "chunk_index": r.chunk_index,
                        "line_start": r.line_start,
                        "line_end": r.line_end,
                        "keyword_score": round(r.keyword_score, 4) if r.keyword_score else None,
                        "vector_score": round(r.vector_score, 4) if r.vector_score else None,
                        "graph_score": round(r.graph_score, 4) if r.graph_score else None,
                        "graph_context": r.graph_context.to_dict() if r.graph_context else None,
                    }
                    for r in results
                ],
                "total": len(results),
                "latency_ms": round(latency_ms, 2),
            }
            if routing_info:
                response["routing"] = routing_info
            return response

        # Standard search (effective_graph_mode="none")
        results = await search_daemon.search(
            query=q,
            search_type=type,
            limit=effective_limit,
            path_filter=path,
            alpha=alpha,
            fusion_method=fusion,
            adaptive_k=adaptive_k,
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        response = {
            "query": q,
            "search_type": type,
            "graph_mode": "none",
            "results": [
                {
                    "path": r.path,
                    "chunk_text": r.chunk_text,
                    "score": round(r.score, 4),
                    "chunk_index": r.chunk_index,
                    "line_start": r.line_start,
                    "line_end": r.line_end,
                    "keyword_score": round(r.keyword_score, 4) if r.keyword_score else None,
                    "vector_score": round(r.vector_score, 4) if r.vector_score else None,
                }
                for r in results
            ],
            "total": len(results),
            "latency_ms": round(latency_ms, 2),
        }
        if routing_info:
            response["routing"] = routing_info
        return response

    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search error: {e}") from e


@router.post("/api/search/refresh")
async def search_refresh_notify(
    path: str = Query(..., description="Path of the changed file"),
    change_type: str = Query("update", description="Type of change: create, update, delete"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(get_search_daemon),
) -> dict[str, Any]:
    """Notify the search daemon of a file change for index refresh.

    This endpoint allows external systems to trigger index updates
    when files are modified outside of the normal Nexus write flow.

    Args:
        path: Virtual path of the changed file
        change_type: Type of change (create, update, delete)

    Returns:
        Acknowledgment of the notification
    """
    await search_daemon.notify_file_change(path, change_type)

    return {
        "status": "accepted",
        "path": path,
        "change_type": change_type,
    }


@router.post("/api/search/expand")
async def search_expand(
    q: str = Query(..., description="Query to expand", min_length=1),
    context: str | None = Query(None, description="Optional context about the collection"),
    model: str = Query("deepseek/deepseek-chat", description="LLM model to use"),
    max_lex: int = Query(2, description="Max lexical variants", ge=0, le=5),
    max_vec: int = Query(2, description="Max vector variants", ge=0, le=5),
    max_hyde: int = Query(2, description="Max HyDE passages", ge=0, le=5),
    _auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Expand a search query using LLM-based query expansion (Issue #1174).

    Generates multiple query variants to improve search recall:
    - lex: Lexical variants (keywords for BM25)
    - vec: Vector variants (natural language for embeddings)
    - hyde: Hypothetical document passages

    Requires OPENROUTER_API_KEY environment variable.

    Args:
        q: The query to expand
        context: Optional context about the document collection
        model: LLM model to use (default: deepseek/deepseek-chat)
        max_lex: Maximum lexical variants (0-5)
        max_vec: Maximum vector variants (0-5)
        max_hyde: Maximum HyDE passages (0-5)

    Returns:
        Query expansions with metadata
    """
    import os

    from nexus.search.query_expansion import (
        OpenRouterQueryExpander,
        QueryExpansionConfig,
    )

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENROUTER_API_KEY not configured for query expansion",
        )

    start_time = time.perf_counter()

    try:
        config = QueryExpansionConfig(
            model=model,
            max_lex_variants=max_lex,
            max_vec_variants=max_vec,
            max_hyde_passages=max_hyde,
            timeout=15.0,
        )
        expander = OpenRouterQueryExpander(config=config, api_key=api_key)

        expansions = await expander.expand(q, context=context)
        await expander.close()

        latency_ms = (time.perf_counter() - start_time) * 1000

        return {
            "query": q,
            "context": context,
            "model": model,
            "expansions": [
                {
                    "type": e.expansion_type.value,
                    "text": e.text,
                    "weight": e.weight,
                }
                for e in expansions
            ],
            "total": len(expansions),
            "latency_ms": round(latency_ms, 2),
        }

    except Exception as e:
        logger.error(f"Query expansion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Query expansion error: {e}") from e
