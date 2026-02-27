"""Search API v2 router (#2056).

Provides search daemon endpoints:
- GET  /api/v2/search/health   -- daemon health check (public, no auth)
- GET  /api/v2/search/stats    -- daemon statistics
- GET  /api/v2/search/query    -- execute search query
- POST /api/v2/search/refresh  -- notify daemon of file change
- POST /api/v2/search/expand   -- LLM-based query expansion

Ported from v1 with improvements:
- Top-level imports for QueryRouter, graph_enhanced_search
- Generic error messages (don't leak internal details)
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/search", tags=["search"])

# =============================================================================
# Dependencies
# =============================================================================


def _get_search_daemon(request: Request) -> Any:
    """Get SearchDaemon from app.state, raising 503 if not enabled."""
    daemon = getattr(request.app.state, "search_daemon", None)
    if daemon is None:
        raise HTTPException(
            status_code=503,
            detail="Search daemon not enabled (set NEXUS_SEARCH_DAEMON=true)",
        )
    return daemon


def _get_record_store(request: Request) -> Any:
    """Get RecordStore from app.state."""
    store = getattr(request.app.state, "record_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Record store not available")
    return store


def _get_optional_search_daemon(request: Request) -> Any:
    """Get SearchDaemon from app.state, returning None if not enabled."""
    return getattr(request.app.state, "search_daemon", None)


def _get_async_read_session_factory(request: Request) -> Any:
    """Get async read session factory for read-only operations."""
    factory = getattr(request.app.state, "async_read_session_factory", None)
    if factory is not None:
        return factory
    factory = getattr(request.app.state, "async_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail="Async session factory not available (RecordStore not configured)",
        )
    return factory


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/health")
async def search_daemon_health(
    search_daemon: Any = Depends(_get_optional_search_daemon),
) -> dict[str, Any]:
    """Health check for the search daemon."""
    if not search_daemon:
        return {
            "status": "disabled",
            "daemon_enabled": False,
            "message": "Search daemon not enabled (set NEXUS_SEARCH_DAEMON=true)",
        }
    health: dict[str, Any] = search_daemon.get_health()
    return health


@router.get("/stats")
async def search_daemon_stats(
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Get search daemon statistics."""
    stats: dict[str, Any] = search_daemon.get_stats()
    return stats


@router.get("/query")
async def search_query(
    q: str = Query(..., description="Search query text", min_length=1),
    type: str = Query("hybrid", description="Search type: keyword, semantic, or hybrid"),
    limit: int = Query(10, description="Maximum number of results", ge=1, le=100),
    path: str | None = Query(None, description="Optional path prefix filter"),
    alpha: float = Query(0.5, description="Semantic vs keyword weight (0.0-1.0)", ge=0.0, le=1.0),
    fusion: str = Query("rrf", description="Fusion method: rrf, weighted, or rrf_weighted"),
    adaptive_k: bool = Query(False, description="Adaptive retrieval"),
    graph_mode: str = Query(
        "none", description="Graph enhancement mode: none, low, high, dual, auto"
    ),
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
    async_session_factory: Any = Depends(_get_async_read_session_factory),
    record_store: Any = Depends(_get_record_store),
) -> dict[str, Any]:
    """Execute a fast search query using the search daemon."""
    from nexus.bricks.search.query_router import QueryRouter
    from nexus.contracts.constants import ROOT_ZONE_ID

    start_time = time.perf_counter()
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    if not search_daemon.is_initialized:
        raise HTTPException(status_code=503, detail="Search daemon is still initializing")

    if type not in ("keyword", "semantic", "hybrid"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid search type: {type}. Must be 'keyword', 'semantic', or 'hybrid'",
        )

    if fusion not in ("rrf", "weighted", "rrf_weighted"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid fusion method: {fusion}. Must be 'rrf', 'weighted', or 'rrf_weighted'",
        )

    if graph_mode not in ("none", "low", "high", "dual", "auto"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid graph_mode: {graph_mode}. Must be 'none', 'low', 'high', 'dual', or 'auto'",
        )

    routing_info: dict[str, Any] | None = None
    effective_graph_mode = graph_mode
    effective_limit = limit

    if graph_mode == "auto":
        query_router = QueryRouter()
        routed = query_router.route(q, base_limit=limit)
        effective_graph_mode = routed.graph_mode
        effective_limit = routed.adjusted_limit
        routing_info = routed.to_dict()
        logger.info(
            "[QUERY-ROUTER] %s, graph_mode=%s, limit=%s",
            routed.reasoning,
            effective_graph_mode,
            effective_limit,
        )

    try:
        if effective_graph_mode != "none":
            from nexus.services.search.graph_search_service import graph_enhanced_search

            results = await graph_enhanced_search(
                query=q,
                search_type=type,
                limit=effective_limit,
                path_filter=path,
                alpha=alpha,
                graph_mode=effective_graph_mode,
                record_store=record_store,
                async_session_factory=async_session_factory,
                search_daemon=search_daemon,
                zone_id=zone_id,
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

        results = await search_daemon.search(
            query=q,
            search_type=type,
            limit=effective_limit,
            path_filter=path,
            alpha=alpha,
            fusion_method=fusion,
            adaptive_k=adaptive_k,
            zone_id=zone_id,
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
                    "splade_score": round(r.splade_score, 4)
                    if getattr(r, "splade_score", None)
                    else None,
                    "reranker_score": round(r.reranker_score, 4)
                    if getattr(r, "reranker_score", None)
                    else None,
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
        logger.error("Search error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Search query failed") from e


@router.post("/refresh")
async def search_refresh_notify(
    path: str = Query(..., description="Path of the changed file"),
    change_type: str = Query("update", description="Type of change: create, update, delete"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Notify the search daemon of a file change for index refresh."""
    await search_daemon.notify_file_change(path, change_type)
    return {"status": "accepted", "path": path, "change_type": change_type}


@router.post("/bulk-embed")
async def search_bulk_embed(
    batch_size: int = Query(50, description="Batch size for embedding"),
    _auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Trigger bulk embedding of all BM25S-indexed documents.

    Uses BM25S corpus as content source, registers files in file_paths,
    and embeds via the standard IndexingPipeline (decoupled from BM25).
    """
    embedded = await search_daemon.bulk_embed_from_bm25s(batch_size=batch_size)
    return {"status": "completed", "documents_embedded": embedded}


@router.post("/expand")
async def search_expand(
    q: str = Query(..., description="Query to expand", min_length=1),
    context: str | None = Query(None, description="Optional context about the collection"),
    model: str = Query("deepseek/deepseek-chat", description="LLM model to use"),
    max_lex: int = Query(2, description="Max lexical variants", ge=0, le=5),
    max_vec: int = Query(2, description="Max vector variants", ge=0, le=5),
    max_hyde: int = Query(2, description="Max HyDE passages", ge=0, le=5),
    _auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Expand a search query using LLM-based query expansion."""
    import os

    from nexus.bricks.search.query_expansion import (
        OpenAIQueryExpander,
        OpenRouterQueryExpander,
        QueryExpansionConfig,
    )

    # Try OpenRouter first, fall back to OpenAI
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openrouter_key and not openai_key:
        raise HTTPException(
            status_code=503,
            detail="No API key configured for query expansion (need OPENROUTER_API_KEY or OPENAI_API_KEY)",
        )

    start_time = time.perf_counter()

    try:
        if openrouter_key:
            config = QueryExpansionConfig(
                model=model,
                max_lex_variants=max_lex,
                max_vec_variants=max_vec,
                max_hyde_passages=max_hyde,
                timeout=15.0,
            )
            expander = OpenRouterQueryExpander(config=config, api_key=openrouter_key)
        else:
            # Use OpenAI directly with gpt-4o-mini
            openai_model = model if "/" not in model else "gpt-4o-mini"
            config = QueryExpansionConfig(
                model=openai_model,
                max_lex_variants=max_lex,
                max_vec_variants=max_vec,
                max_hyde_passages=max_hyde,
                timeout=15.0,
                fallback_models=[],
            )
            expander = OpenAIQueryExpander(config=config, api_key=openai_key)
        expansions = await expander.expand(q, context=context)
        await expander.close()

        latency_ms = (time.perf_counter() - start_time) * 1000

        return {
            "query": q,
            "context": context,
            "model": model,
            "expansions": [
                {"type": e.expansion_type.value, "text": e.text, "weight": e.weight}
                for e in expansions
            ],
            "total": len(expansions),
            "latency_ms": round(latency_ms, 2),
        }

    except Exception as e:
        logger.error("Query expansion error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Query expansion failed") from e
