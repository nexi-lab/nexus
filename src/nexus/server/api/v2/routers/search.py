"""Search API v2 router (#2056, #2663).

Provides search daemon endpoints:
- GET  /api/v2/search/health   -- daemon health check (public, no auth)
- GET  /api/v2/search/stats    -- daemon statistics
- GET  /api/v2/search/query    -- execute search query
- POST /api/v2/search/index    -- explicit document indexing
- POST /api/v2/search/refresh  -- notify daemon of file change
- POST /api/v2/search/expand   -- LLM-based query expansion

Rewritten for txtai backend (#2663):
- txtai handles hybrid BM25+dense fusion internally
- Zone-level isolation via txtai SQL WHERE (brick layer)
- File-level ReBAC filtering in router (server layer)
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
# ReBAC filtering helper
# =============================================================================


def _normalize_path(path: str) -> str:
    """Ensure path is absolute for ReBAC filter_list compatibility."""
    if not path.startswith("/"):
        return f"/{path}"
    return path


def _apply_rebac_filter(
    results: list[Any],
    permission_enforcer: Any | None,
    auth_result: dict[str, Any],
    zone_id: str,
) -> tuple[list[Any], float]:
    """Apply ReBAC file-level permission filtering to search results.

    Returns (filtered_results, filter_time_ms).

    Uses individual rebac_check() calls instead of filter_list() because:
    1. filter_list() runs the NamespaceManager pre-filter which requires
       mount table entries — search paths don't have mount entries.
    2. rebac_check_bulk() routes through Rust acceleration which has a bug
       that returns all-False despite correct tuples in the DB.
    3. For search (10-30 results), individual checks are fast enough (~5ms/path).
    """
    if permission_enforcer is None:
        return results, 0.0

    rebac_manager = getattr(permission_enforcer, "rebac_manager", None)
    if rebac_manager is None or not hasattr(rebac_manager, "rebac_check"):
        return results, 0.0

    user_id = auth_result.get("subject_id") or auth_result.get("user_id", "anonymous")
    subject = ("user", user_id)

    # Build path→normalized lookup; ReBAC requires absolute paths
    path_map = {_normalize_path(r.path): r.path for r in results}
    abs_paths = list(path_map.keys())

    logger.debug(
        "[SEARCH-REBAC] rebac_check: user_id=%s, zone_id=%s, is_admin=%s, paths=%d",
        user_id,
        zone_id,
        auth_result.get("is_admin", False),
        len(abs_paths),
    )

    filter_start = time.perf_counter()
    permitted_abs: set[str] = set()
    try:
        for abs_path in abs_paths:
            allowed = rebac_manager.rebac_check(
                subject=subject,
                permission="read",
                object=("file", abs_path),
                zone_id=zone_id,
            )
            if allowed:
                permitted_abs.add(abs_path)
    except Exception:
        logger.warning("ReBAC rebac_check failed, denying all results (fail-closed)", exc_info=True)
        filter_ms = (time.perf_counter() - filter_start) * 1000
        return [], filter_ms
    filter_ms = (time.perf_counter() - filter_start) * 1000

    logger.debug("[SEARCH-REBAC] permitted %d/%d paths", len(permitted_abs), len(abs_paths))

    # Map back: allowed absolute paths → original result paths
    permitted_originals = {path_map[p] for p in permitted_abs if p in path_map}
    filtered = [r for r in results if r.path in permitted_originals]
    return filtered, filter_ms


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
    request: Request,
    q: str = Query(..., description="Search query text", min_length=1),
    type: str = Query("hybrid", description="Search type: keyword, semantic, or hybrid"),
    limit: int = Query(10, description="Maximum number of results", ge=1, le=100),
    path: str | None = Query(None, description="Optional path prefix filter"),
    alpha: float = Query(0.5, description="Semantic vs keyword weight (0.0-1.0)", ge=0.0, le=1.0),
    fusion: str = Query("rrf", description="Fusion method: rrf, weighted, or rrf_weighted"),
    adaptive_k: bool = Query(False, description="Adaptive retrieval"),
    rerank: bool | None = Query(
        None, description="Override reranker (true/false, default: use config)"
    ),
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

    # ReBAC file-level permission enforcer (Decision #17)
    permission_enforcer = getattr(request.app.state, "permission_enforcer", None)

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

    # Over-fetch when permission filtering is active to compensate for filtered results
    fetch_limit = effective_limit
    if permission_enforcer is not None:
        fetch_limit = effective_limit * 3

    try:
        filter_ms = 0.0

        if effective_graph_mode != "none":
            from nexus.services.search.graph_search_service import graph_enhanced_search

            results = await graph_enhanced_search(
                query=q,
                search_type=type,
                limit=fetch_limit,
                path_filter=path,
                alpha=alpha,
                graph_mode=effective_graph_mode,
                record_store=record_store,
                async_session_factory=async_session_factory,
                search_daemon=search_daemon,
                zone_id=zone_id,
            )

            # ReBAC file-level filtering (Decision #17)
            results, filter_ms = _apply_rebac_filter(
                results, permission_enforcer, auth_result, zone_id
            )
            results = results[:effective_limit]

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
                    }
                    for r in results
                ],
                "total": len(results),
                "latency_ms": round(latency_ms, 2),
                "latency_breakdown": {
                    "total_ms": round(latency_ms, 2),
                    "permission_filter_ms": round(filter_ms, 2),
                },
            }
            if routing_info:
                response["routing"] = routing_info
            return response

        results = await search_daemon.search(
            query=q,
            search_type=type,
            limit=fetch_limit,
            path_filter=path,
            alpha=alpha,
            fusion_method=fusion,
            adaptive_k=adaptive_k,
            zone_id=zone_id,
            rerank=rerank,
        )

        # Read sub-timings from daemon
        daemon_timing = getattr(search_daemon, "last_search_timing", {})
        backend_ms = daemon_timing.get("backend_ms", 0.0)
        rerank_ms = daemon_timing.get("rerank_ms", 0.0)

        # ReBAC file-level filtering (Decision #17)
        results, filter_ms = _apply_rebac_filter(results, permission_enforcer, auth_result, zone_id)
        results = results[:effective_limit]

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
                    if r.splade_score is not None
                    else None,
                    "reranker_score": round(r.reranker_score, 4)
                    if r.reranker_score is not None
                    else None,
                }
                for r in results
            ],
            "total": len(results),
            "latency_ms": round(latency_ms, 2),
            "latency_breakdown": {
                "total_ms": round(latency_ms, 2),
                "backend_ms": round(backend_ms, 2),
                "rerank_ms": round(rerank_ms, 2),
                "permission_filter_ms": round(filter_ms, 2),
            },
        }
        if routing_info:
            response["routing"] = routing_info
        return response

    except Exception as e:
        logger.error("Search error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Search query failed") from e


@router.post("/index")
async def search_index_documents(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Explicitly index documents (Decision #18: call-by-call indexing).

    Request body: ``{"documents": [{"id": str, "text": str, "path": str, ...}]}``
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    body = await request.json()
    documents: list[dict[str, Any]] = body.get("documents", [])
    if not documents:
        raise HTTPException(status_code=400, detail="No documents provided")

    count = await search_daemon.index_documents(documents, zone_id=zone_id)
    return {"status": "indexed", "count": count, "zone_id": zone_id}


@router.post("/refresh")
async def search_refresh_notify(
    path: str = Query(..., description="Path of the changed file"),
    change_type: str = Query("update", description="Type of change: create, update, delete"),
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Notify the search daemon of a file change for index refresh."""
    from nexus.contracts.constants import ROOT_ZONE_ID

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    await search_daemon.notify_file_change(path, change_type, zone_id=zone_id)
    return {"status": "accepted", "path": path, "change_type": change_type}


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
