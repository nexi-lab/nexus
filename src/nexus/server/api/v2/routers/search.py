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
            detail="Search daemon unavailable (set NEXUS_SEARCH_DAEMON=false to disable)",
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

    Uses PermissionEnforcer.filter_search_results() which delegates to
    rebac_list_objects() (Rust-accelerated, 1 SQL query + 1 Rust computation).
    This bypasses the NamespaceManager pre-filter (search paths lack mount entries)
    and avoids the stale graph cache bug in compute_permissions_bulk.
    """
    if permission_enforcer is None:
        return results, 0.0

    if not hasattr(permission_enforcer, "filter_search_results"):
        return results, 0.0

    user_id = auth_result.get("subject_id") or auth_result.get("user_id", "anonymous")
    is_admin = bool(auth_result.get("is_admin", False))

    # Normalize paths to absolute for ReBAC compatibility
    path_map = {_normalize_path(r.path): r for r in results}
    abs_paths = list(path_map.keys())

    filter_start = time.perf_counter()
    permitted_abs = permission_enforcer.filter_search_results(
        abs_paths,
        user_id=user_id,
        zone_id=zone_id,
        is_admin=is_admin,
    )
    filter_ms = (time.perf_counter() - filter_start) * 1000

    logger.debug(
        "[SEARCH-REBAC] permitted %d/%d paths in %.1fms",
        len(permitted_abs),
        len(abs_paths),
        filter_ms,
    )

    permitted_set = set(permitted_abs)
    filtered = [path_map[p] for p in abs_paths if p in permitted_set]
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
            "message": "Search daemon unavailable (set NEXUS_SEARCH_DAEMON=false to disable)",
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
    rerank: bool | None = Query(  # noqa: ARG001
        None, description="Override reranker (true/false, default: use config)"
    ),
    graph_mode: str = Query(
        "none", description="Graph enhancement mode: none, low, high, dual, auto"
    ),
    federated: bool = Query(False, description="Cross-zone federated search (Issue #3147)"),
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

    # --- Federated search path (Issue #3147) ---
    if federated:
        return await _handle_federated_search(
            q=q,
            search_type=type,
            limit=limit,
            path_filter=path,
            alpha=alpha,
            fusion_method=fusion,
            auth_result=auth_result,
            search_daemon=search_daemon,
            request=request,
        )

    # --- Standard single-zone search path ---
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
            from nexus.bricks.search.graph_search_service import graph_enhanced_search

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
            zone_id=zone_id,
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


async def _handle_federated_search(
    *,
    q: str,
    search_type: str,
    limit: int,
    path_filter: str | None,
    alpha: float,
    fusion_method: str,
    auth_result: dict[str, Any],
    search_daemon: Any,
    request: Request,
) -> dict[str, Any]:
    """Handle federated cross-zone search (Issue #3147).

    Delegates to FederatedSearchDispatcher which fans out search
    across all accessible zones and fuses results via raw score merge.
    """
    from nexus.bricks.search.federated_search import FederatedSearchDispatcher

    # Resolve ReBAC service
    rebac = getattr(request.app.state, "rebac_service", None)
    if rebac is None:
        raw_mgr = getattr(request.app.state, "rebac_manager", None)
        if raw_mgr is not None:
            from nexus.bricks.rebac.rebac_service import ReBACService

            rebac = ReBACService(raw_mgr)
            request.app.state.rebac_service = rebac
    if rebac is None:
        raise HTTPException(status_code=503, detail="Federated search requires ReBAC service")

    user_id = auth_result.get("user_id", "")
    subject_type = auth_result.get("subject_type", "user")
    subject_id = auth_result.get("subject_id") or user_id
    subject = (subject_type, subject_id)

    registry = getattr(request.app.state, "zone_search_registry", None)
    per_file_rebac = getattr(request.app.state, "federated_per_file_rebac", True)
    dispatcher = FederatedSearchDispatcher(
        daemon=search_daemon,
        rebac=rebac,
        registry=registry,
        enable_per_file_rebac=per_file_rebac,
    )
    fed_response = await dispatcher.search(
        query=q,
        subject=subject,
        search_type=search_type,
        limit=limit,
        path_filter=path_filter,
        alpha=alpha,
        fusion_method=fusion_method,
    )

    response_dict: dict[str, Any] = {
        "query": q,
        "search_type": search_type,
        "graph_mode": "none",
        "federated": True,
        "results": fed_response.results,
        "total": len(fed_response.results),
        "latency_ms": round(fed_response.latency_ms, 2),
        "zones_searched": fed_response.zones_searched,
        "zones_failed": [
            {"zone_id": zf.zone_id, "error": zf.error} for zf in fed_response.zones_failed
        ],
    }
    if fed_response.zones_skipped:
        response_dict["zones_skipped"] = fed_response.zones_skipped
    if fed_response.cached:
        response_dict["cached"] = True
    return response_dict


@router.post("/query/batch")
async def search_query_batch(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Batch search: run N queries through full hybrid pipeline.

    Body: {
        "queries": [
            {"q": "text", "limit": 10, "path": "/optional"},
            ...
        ]
    }

    Returns: {"queries": [{"query": str, "results": [...], "total": int}, ...]}

    Applies the same ReBAC file-level permission filter as the single-query
    ``/query`` endpoint (Decision #17). Each query is over-fetched 3x when
    the permission enforcer is active and trimmed to its configured ``limit``
    after filtering so authorized results are not starved by denied paths.

    Optimized for benchmarks and bulk evaluations. txtai's batchsearch()
    embeds all query texts in ONE OpenAI API call, then runs each through
    the full hybrid pipeline (BM25 + vector + fusion). For 470 queries:
    ~30s instead of ~16 min sequential.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    body = await request.json()
    raw_queries: list[dict[str, Any]] = body.get("queries", [])
    if not raw_queries:
        raise HTTPException(status_code=400, detail="No queries provided")

    if not search_daemon.is_initialized:
        raise HTTPException(status_code=503, detail="Search daemon is still initializing")

    # Same ReBAC hook the single-query endpoint uses.
    permission_enforcer = getattr(request.app.state, "permission_enforcer", None)
    overfetch_multiplier = 3 if permission_enforcer is not None else 1

    # Over-fetch per-query so ReBAC filtering does not strip us below the
    # caller's requested limit. Keep caller's original limit for trimming.
    requested_limits: list[int] = []
    fetch_queries: list[dict[str, Any]] = []
    for q_spec in raw_queries:
        orig_limit = max(1, int(q_spec.get("limit", 10)))
        requested_limits.append(orig_limit)
        fetch_queries.append({**q_spec, "limit": orig_limit * overfetch_multiplier})

    t0 = time.perf_counter()
    raw_results = await search_daemon.batch_search(fetch_queries, zone_id=zone_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    filter_ms_total = 0.0
    response_queries: list[dict[str, Any]] = []
    for q_spec, results, orig_limit in zip(raw_queries, raw_results, requested_limits, strict=True):
        # File-level ReBAC filtering (Decision #17) — same enforcement as /query.
        filtered, filter_ms = _apply_rebac_filter(
            results, permission_enforcer, auth_result, zone_id
        )
        filter_ms_total += filter_ms
        trimmed = filtered[:orig_limit]

        formatted = [
            {
                "path": r.path,
                "chunk_text": r.chunk_text,
                "score": round(r.score, 4),
                "keyword_score": round(r.keyword_score, 4) if r.keyword_score is not None else None,
                "vector_score": round(r.vector_score, 4) if r.vector_score is not None else None,
            }
            for r in trimmed
        ]
        response_queries.append(
            {
                "query": q_spec.get("q", ""),
                "results": formatted,
                "total": len(formatted),
            }
        )

    return {
        "queries": response_queries,
        "total_queries": len(raw_queries),
        "latency_ms": round(elapsed_ms, 2),
        "avg_per_query_ms": round(elapsed_ms / max(len(raw_queries), 1), 2),
        "permission_filter_ms": round(filter_ms_total, 2),
    }


@router.post("/index")
async def search_index_documents(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Explicitly index documents (Decision #18: call-by-call indexing).

    Request body: ``{"documents": [{"id": str, "text": str, "path": str, ...}]}``

    Fails closed with HTTP 500 if the underlying backend cannot persist
    (e.g., config path unwritable, PostgreSQL commit failed), so clients
    can retry instead of silently losing data.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    body = await request.json()
    documents: list[dict[str, Any]] = body.get("documents", [])
    if not documents:
        raise HTTPException(status_code=400, detail="No documents provided")

    try:
        count = await search_daemon.index_documents(documents, zone_id=zone_id)
    except Exception as exc:
        logger.error("index_documents failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Index persistence failed: {type(exc).__name__}: {exc}",
        ) from exc
    return {"status": "indexed", "count": count, "zone_id": zone_id}


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


# =============================================================================
# Per-directory semantic index scoping (Issue #3698)
# =============================================================================
#
# Endpoints to opt a zone's directories into the embedding pipeline. When a
# zone is in ``'scoped'`` mode, only files under registered directories are
# embedded; BM25/FTS/Zoekt keep full coverage regardless.
#
# Authorization policy (Issue #6 #7): the caller must either be admin or
# hold ReBAC write permission on the directory path. A full ReBAC integration
# will arrive alongside the feature; this v1 uses admin-only so we don't
# couple the search layer to the permission enforcer's async API today.


async def _require_admin_or_path_write(
    request: Request,
    auth_result: dict[str, Any],
    zone_id: str,
    directory_path: str,
) -> None:
    """Policy gate for directory-scope mutation endpoints (Issue #3698 #6.7).

    Policy: admin bypass, otherwise require write permission on the target
    path via the (sync) ``permission_enforcer`` wired onto ``app.state``.
    If no enforcer is available, deny (fail-closed) — a deployment without
    a permission enforcer should be admin-only for mutation endpoints.
    """
    if auth_result.get("is_admin", False):
        return

    enforcer = getattr(request.app.state, "permission_enforcer", None)
    if enforcer is None:
        # Fail closed — no enforcer wired means non-admins cannot mutate
        # index scope. Admins already bypassed above.
        raise HTTPException(
            status_code=403,
            detail="index scope mutation requires admin privileges in this deployment",
        )

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext, Permission

    ctx = OperationContext(
        user_id=auth_result.get("subject_id", ""),
        groups=auth_result.get("groups", []),
        zone_id=zone_id or ROOT_ZONE_ID,
        is_admin=False,
        subject_type=auth_result.get("subject_type", "user"),
        subject_id=auth_result.get("subject_id"),
    )
    try:
        # PermissionEnforcer.check is sync — no await.
        allowed = bool(enforcer.check(directory_path, Permission.WRITE, ctx))
    except Exception as exc:
        logger.warning("ReBAC write check failed for %s: %s", directory_path, exc)
        raise HTTPException(status_code=500, detail="permission check failed") from exc

    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"write permission required on {directory_path}",
        )


@router.post("/index-directory")
async def register_indexed_directory(
    request: Request,
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Register a directory for scoped semantic indexing (Issue #3698).

    Request body:
        {"path": "/zone/zone_a/project/src"}  -- or any canonical virtual path

    Policies (Issue #6):
    - Non-existent directories are ALLOWED (register for future use).
    - File paths (non-directory) are rejected — the daemon does not verify
      this today; v1 trusts the caller. Filesystem existence check is a
      follow-up.
    - Path escapes (``..``) → 400.
    - Missing zone → 404.
    - Duplicate registration → 409.
    """
    from nexus.bricks.search.index_scope import (
        DirectoryAlreadyRegisteredError,
        InvalidDirectoryPathError,
        ZoneNotFoundError,
    )
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    directory_path = payload.get("path")
    if not isinstance(directory_path, str) or not directory_path:
        raise HTTPException(status_code=400, detail="'path' field is required")

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    await _require_admin_or_path_write(request, auth_result, zone_id, directory_path)

    try:
        canonical = await search_daemon.add_indexed_directory(zone_id, directory_path)
    except ZoneNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DirectoryAlreadyRegisteredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "zone_id": zone_id,
        "path": canonical,
        "status": "registered",
    }


@router.delete("/index-directory")
async def unregister_indexed_directory(
    request: Request,
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Unregister a directory from scoped semantic indexing (Issue #3698).

    Does NOT purge existing embeddings — use ``/purge-unscoped`` for that.
    Returns 404 if the directory was not registered.
    """
    from nexus.bricks.search.index_scope import (
        DirectoryNotRegisteredError,
        InvalidDirectoryPathError,
    )
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    directory_path = payload.get("path")
    if not isinstance(directory_path, str) or not directory_path:
        raise HTTPException(status_code=400, detail="'path' field is required")

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    await _require_admin_or_path_write(request, auth_result, zone_id, directory_path)

    try:
        canonical = await search_daemon.remove_indexed_directory(zone_id, directory_path)
    except DirectoryNotRegisteredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "zone_id": zone_id,
        "path": canonical,
        "status": "unregistered",
    }


@router.get("/indexed-dirs")
async def list_indexed_dirs(
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """List directories currently registered for scoped semantic indexing.

    Returns an empty list if no directories are registered for the caller's
    zone (which, combined with zone mode 'all', means the zone indexes
    everything).
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    mode = search_daemon._zone_indexing_modes.get(zone_id, "all")
    directories = search_daemon.list_indexed_directories(zone_id)
    return {
        "zone_id": zone_id,
        "indexing_mode": mode,
        "directories": directories,
    }


@router.post("/indexing-mode")
async def set_indexing_mode(
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Flip a zone between ``'all'`` and ``'scoped'`` indexing modes (Issue #3698).

    Admin-only. Takes effect immediately — the daemon updates its
    in-memory state under ``_refresh_lock`` alongside the DB write.

    Request body:
        {"mode": "all" | "scoped", "zone_id": "optional — defaults to caller's zone"}
    """
    from nexus.bricks.search.index_scope import (
        InvalidDirectoryPathError,
        ZoneNotFoundError,
    )
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(status_code=403, detail="set-indexing-mode is admin-only")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    mode = payload.get("mode")
    if not isinstance(mode, str) or not mode:
        raise HTTPException(status_code=400, detail="'mode' field is required")

    zone_id = payload.get("zone_id") or auth_result.get("zone_id") or ROOT_ZONE_ID

    try:
        await search_daemon.set_zone_indexing_mode(zone_id, mode)
    except ZoneNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"zone_id": zone_id, "indexing_mode": mode, "status": "updated"}


@router.post("/purge-unscoped")
async def purge_unscoped_embeddings(
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Admin-only: purge stored embeddings for files outside any scope.

    Destructive operation (Issue #3698). Call this after flipping a zone
    from ``'all'`` to ``'scoped'`` or after unregistering directories to
    clean up stale embeddings and txtai docs for files that are no longer
    in scope. Only zones in ``'scoped'`` mode are affected.

    This is decoupled from the unregister endpoint intentionally: removing
    a directory registration is low-risk, purging embeddings is high-risk.
    Keep them separate to minimize the blast radius of misconfiguration.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="purge-unscoped is admin-only",
        )

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    counts = await search_daemon.purge_unscoped_embeddings(zone_id)
    return {
        "zone_id": zone_id,
        "purged": counts,
    }
