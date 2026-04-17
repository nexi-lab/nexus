"""Search API v2 router (#2056, #2663, #3701).

Provides search daemon endpoints:
- GET  /api/v2/search/health   -- daemon health check (public, no auth)
- GET  /api/v2/search/stats    -- daemon statistics
- GET  /api/v2/search/query    -- execute search query
- GET  /api/v2/search/grep     -- search file contents (#3701, small files=)
- POST /api/v2/search/grep     -- same as GET, JSON body for large files=
- GET  /api/v2/search/glob     -- search files by pattern (#3701, small files=)
- POST /api/v2/search/glob     -- same as GET, JSON body for large files=
- POST /api/v2/search/index    -- explicit document indexing
- POST /api/v2/search/refresh  -- notify daemon of file change
- POST /api/v2/search/expand   -- LLM-based query expansion

Rewritten for txtai backend (#2663):
- txtai handles hybrid BM25+dense fusion internally
- Zone-level isolation via txtai SQL WHERE (brick layer)
- File-level ReBAC filtering in router (server layer)

#3701 review:
- Added grep/glob HTTP endpoints (previously MCP-only).
- Collapsed duplicated response shaping into ``_serialize_search_result``.
- Replaced the 3x over-fetch magic number with ``_REBAC_OVERFETCH_FACTOR``
  and added ``truncated_by_permissions`` / ``permission_denial_rate``
  instrumentation so callers can detect silent-undercount scenarios.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.lib.pagination import build_paginated_list_response
from nexus.lib.rebac_filter import apply_rebac_filter as _apply_rebac_filter
from nexus.lib.rebac_filter import compute_rebac_fetch_limit as _compute_rebac_fetch_limit
from nexus.lib.rebac_filter import rebac_denial_stats as _rebac_denial_stats
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/search", tags=["search"])

# =============================================================================
# Constants (#3701 review — Issue 16A)
# =============================================================================

# When a permission enforcer is active we over-fetch to compensate for
# results that will be stripped during ReBAC filtering. 3x is the legacy
# value chosen empirically when #2056 landed. Beware: when the denial rate
# exceeds ~66% this factor is insufficient and the response reports
# ``truncated_by_permissions`` so callers can detect the silent undercount.

# ReBAC constants and helpers are now in nexus.lib.rebac_filter (#3731).

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


# ReBAC filtering helpers are in nexus.lib.rebac_filter (#3731).

# =============================================================================
# Response shaping helpers (#3701 review — Issue 5A)
# =============================================================================


def _serialize_search_result(result: Any) -> dict[str, Any]:
    """Serialize a single search result into the canonical response dict.

    Collapses the 25-line dict comprehension previously duplicated across
    the graph and non-graph branches of ``search_query``. Preserves the
    pre-refactor field ordering, rounding, and None semantics.

    Issue #3773: emits ``context`` when the result carries a non-None value
    (omits the key otherwise to keep responses compact).
    """
    out: dict[str, Any] = {
        "path": result.path,
        "chunk_text": result.chunk_text,
        "score": round(result.score, 4),
        "chunk_index": result.chunk_index,
        "line_start": result.line_start,
        "line_end": result.line_end,
        "keyword_score": (round(result.keyword_score, 4) if result.keyword_score else None),
        "vector_score": (round(result.vector_score, 4) if result.vector_score else None),
    }
    splade = getattr(result, "splade_score", None)
    out["splade_score"] = round(splade, 4) if splade is not None else None
    reranker = getattr(result, "reranker_score", None)
    out["reranker_score"] = round(reranker, 4) if reranker is not None else None
    context = getattr(result, "context", None)
    if context is not None:
        out["context"] = context
    return out


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

    # Coerce graph_mode to 'none' when the txtai backend has graph
    # disabled (the default — see DaemonConfig.txtai_graph). Without
    # this, an explicit graph_mode=low|high|dual|auto request would
    # silently fall through to ``graph_search`` which returns ``[]``
    # for empty graph state, regressing to zero results instead of
    # ordinary hybrid search. We log a warning so operators can flip
    # ``NEXUS_TXTAI_GRAPH=true`` if they actually need graph queries.
    _txtai_graph_enabled = bool(
        getattr(getattr(search_daemon, "config", None), "txtai_graph", False)
    )
    if effective_graph_mode != "none" and not _txtai_graph_enabled:
        logger.info(
            "graph_mode=%s requested but txtai graph is disabled; "
            "falling back to graph_mode=none. Set NEXUS_TXTAI_GRAPH=true "
            "to enable graph-augmented search.",
            effective_graph_mode,
        )
        effective_graph_mode = "none"

    # Over-fetch when permission filtering is active to compensate for
    # filtered results (#3701 review: Issue 16A — replaces the 3x magic
    # number with a named constant and adds silent-undercount detection).
    fetch_limit = _compute_rebac_fetch_limit(
        effective_limit, has_enforcer=permission_enforcer is not None
    )

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
            pre_filter_count = len(results)
            results, filter_ms = _apply_rebac_filter(
                results, permission_enforcer, auth_result, zone_id
            )
            post_filter_count = len(results)
            results = results[:effective_limit]

            latency_ms = (time.perf_counter() - start_time) * 1000

            response: dict[str, Any] = {
                "query": q,
                "search_type": type,
                "graph_mode": effective_graph_mode,
                "results": [_serialize_search_result(r) for r in results],
                "total": len(results),
                "latency_ms": round(latency_ms, 2),
                "latency_breakdown": {
                    "total_ms": round(latency_ms, 2),
                    "permission_filter_ms": round(filter_ms, 2),
                },
                **_rebac_denial_stats(pre_filter_count, post_filter_count, effective_limit),
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
        pre_filter_count = len(results)
        results, filter_ms = _apply_rebac_filter(results, permission_enforcer, auth_result, zone_id)
        post_filter_count = len(results)
        results = results[:effective_limit]

        latency_ms = (time.perf_counter() - start_time) * 1000

        response = {
            "query": q,
            "search_type": type,
            "graph_mode": "none",
            "results": [_serialize_search_result(r) for r in results],
            "total": len(results),
            "latency_ms": round(latency_ms, 2),
            "latency_breakdown": {
                "total_ms": round(latency_ms, 2),
                "backend_ms": round(backend_ms, 2),
                "rerank_ms": round(rerank_ms, 2),
                "permission_filter_ms": round(filter_ms, 2),
            },
            **_rebac_denial_stats(pre_filter_count, post_filter_count, effective_limit),
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

        formatted: list[dict[str, Any]] = []
        for r in trimmed:
            entry: dict[str, Any] = {
                "path": r.path,
                "chunk_text": r.chunk_text,
                "score": round(r.score, 4),
                "keyword_score": round(r.keyword_score, 4) if r.keyword_score is not None else None,
                "vector_score": round(r.vector_score, 4) if r.vector_score is not None else None,
            }
            ctx = getattr(r, "context", None)
            if ctx is not None:
                entry["context"] = ctx
            formatted.append(entry)
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


# =============================================================================
# grep / glob HTTP endpoints (#3701 — Issue 1A)
#
# These endpoints mirror the existing ``nexus_grep``/``nexus_glob`` MCP
# tools but enforce file-level ReBAC via the same ``_apply_rebac_filter``
# helper used by ``search_query``. They are the first time agents can
# get permission-filtered grep/glob results over HTTP.
#
# Implementation notes:
# * Both endpoints delegate to ``SearchService`` via ``nexus_fs.service("search")``
#   because ``SearchDaemon`` does not expose grep/glob methods — those live
#   only at the SearchService layer.
# * ``OperationContext`` is constructed from ``auth_result`` so SearchService's
#   internal path/zone filtering uses the caller's identity.
# * ``_compute_rebac_fetch_limit`` over-fetches from SearchService to
#   compensate for ReBAC denial, matching the pattern in ``search_query``.
# =============================================================================


def _get_search_service(nexus_fs: Any) -> Any:
    """Resolve SearchService from a NexusFS handle.

    Returns the service or raises HTTP 503 if the search brick is absent.
    """
    try:
        service = nexus_fs.service("search")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Search service lookup failed: {exc}") from exc
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Search service not available (search brick not loaded)",
        )
    return service


# =============================================================================
# Shared grep / glob operation helpers (#3701 Issue 1A + POST follow-up)
#
# Both GET and POST handlers delegate to these coroutines so the request
# parsing layer (Query vs JSON body) is separate from the business logic.
# This keeps the POST endpoints trivial (just body → call helper) and
# guarantees GET and POST stay semantically identical forever.
# =============================================================================


async def _do_grep_operation(
    request: Request,
    auth_result: dict[str, Any],
    *,
    pattern: str,
    path: str,
    ignore_case: bool,
    limit: int,
    offset: int,
    before_context: int,
    after_context: int,
    invert_match: bool,
    files: list[str] | None,
    block_type: str | None = None,
) -> dict[str, Any]:
    """Execute a grep request and assemble the paginated response.

    Shared by ``GET /grep`` (query params) and ``POST /grep`` (JSON body).
    Enforces ReBAC at the router layer and surfaces
    ``permission_denial_rate``/``truncated_by_permissions`` in the
    response envelope.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.exceptions import InvalidPathError
    from nexus.server.dependencies import get_operation_context

    start_time = time.perf_counter()
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    nexus_fs = getattr(request.app.state, "nexus_fs", None)
    if nexus_fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    search_service = _get_search_service(nexus_fs)

    # Build OperationContext so SearchService's internal path/zone filter
    # matches the caller's identity (Issue 6A scope: HTTP side).
    op_context = get_operation_context(auth_result)

    permission_enforcer = getattr(request.app.state, "permission_enforcer", None)
    # Sentinel fetch (Codex adversarial review of #3701): request one
    # extra row beyond the caller's window so we can reliably detect
    # whether there are more matches after ReBAC filtering. Without
    # this sentinel, fetching exactly ``limit + offset`` and treating
    # the length as the true total silently reports ``has_more=False``
    # on the first page of a large result set whenever SearchService's
    # cap happens to match the requested window.
    window_size = limit + offset
    sentinel_window = window_size + 1
    fetch_limit = _compute_rebac_fetch_limit(
        sentinel_window, has_enforcer=permission_enforcer is not None
    )

    try:
        grep_kwargs: dict[str, Any] = {
            "pattern": pattern,
            "path": path,
            "ignore_case": ignore_case,
            "max_results": fetch_limit,
            "context": op_context,
            "before_context": before_context,
            "after_context": after_context,
            "invert_match": invert_match,
            "files": files,
        }
        # Issue #3720: only forward block_type when set (backward compat).
        if block_type is not None:
            grep_kwargs["block_type"] = block_type
        raw_results = await search_service.grep(**grep_kwargs)
    except (ValueError, InvalidPathError) as exc:
        # Client errors from SearchService:
        #  * ValueError — invalid regex, size cap exceeded, cross-zone entry
        #  * InvalidPathError — path traversal segment in ``path`` or ``files``
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("grep failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"grep failed: {type(exc).__name__}") from exc

    # ReBAC file-level filtering, reusing the same helper as search_query.
    # SearchService already filters by zone/path via context, so this is
    # a second-layer guarantee for the HTTP surface.
    pre_filter_count = len(raw_results)

    # #3731: path_extractor eliminates the _GrepResultShim shim class.
    filtered_results, filter_ms = _apply_rebac_filter(
        raw_results,
        permission_enforcer,
        auth_result,
        zone_id,
        path_extractor=lambda r: r.get("file", ""),
    )
    post_filter_count = len(filtered_results)

    # Sentinel detection: if we got at least one result beyond the
    # window, there's a next page. The sentinel row is not included in
    # the items we return to the caller.
    has_more = post_filter_count > window_size
    # ``total`` reports the best-known count. When has_more is true,
    # we know at least ``window_size + 1`` exist but the true total
    # may be larger; we report the observed post-filter count as a
    # floor. When has_more is false, post_filter_count is the true
    # total of matches visible to this caller.
    total = post_filter_count
    paginated = filtered_results[offset : offset + limit]

    # Codex review of #3701 (review #2 finding #2 + review #3 finding #3):
    # unscope every result entry's ``file`` so the HTTP response surfaces
    # user-facing paths (``/docs/a.py``) instead of leaking the internal
    # zone-scoped storage path (``/zone/<tenant>/docs/a.py``), but ALSO
    # attach a ``zone_id`` on each item whenever we recovered one from
    # the internal path. A caller with multi-zone visibility (admin or
    # cross-zone share recipient) can then distinguish two results that
    # would otherwise collide onto the same unscoped ``file`` — e.g.
    # ``/zone/acme/src/x.py`` and ``/zone/beta/src/x.py`` both unscope
    # to ``/src/x.py``. Without ``zone_id`` the caller cannot safely
    # round-trip a result back through ``files=[...]``.
    from nexus.core.path_utils import split_zone_from_internal_path

    annotated: list[dict[str, Any]] = []
    for r in paginated:
        out = dict(r)
        raw_file = r.get("file", "")
        zone, unscoped = split_zone_from_internal_path(raw_file)
        out["file"] = unscoped
        if zone is not None:
            out["zone_id"] = zone
        annotated.append(out)
    paginated = annotated

    # Detect residual ambiguity: if two distinct raw paths collapse to
    # the same (file, zone_id) tuple we have a lossy response and
    # surface it in the envelope so callers know round-trip safety is
    # degraded. This is defence-in-depth — the zone_id fix above
    # should already disambiguate every normal multi-zone case.
    _keys = [(it["file"], it.get("zone_id")) for it in paginated]
    multi_zone_ambiguous = len(set(_keys)) < len(_keys)
    latency_ms = (time.perf_counter() - start_time) * 1000

    extras: dict[str, Any] = {
        "latency_ms": round(latency_ms, 2),
        "latency_breakdown": {
            "total_ms": round(latency_ms, 2),
            "permission_filter_ms": round(filter_ms, 2),
        },
        **_rebac_denial_stats(pre_filter_count, post_filter_count, window_size),
    }
    if multi_zone_ambiguous:
        extras["multi_zone_ambiguous"] = True
    return build_paginated_list_response(
        items=paginated,
        total=total,
        offset=offset,
        limit=limit,
        extras=extras,
        has_more=has_more,
    )


async def _do_glob_operation(
    request: Request,
    auth_result: dict[str, Any],
    *,
    pattern: str,
    path: str,
    limit: int,
    offset: int,
    files: list[str] | None,
) -> dict[str, Any]:
    """Execute a glob request and assemble the paginated response.

    Shared by ``GET /glob`` (query params) and ``POST /glob`` (JSON body).
    """
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.exceptions import InvalidPathError
    from nexus.server.dependencies import get_operation_context

    start_time = time.perf_counter()
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    nexus_fs = getattr(request.app.state, "nexus_fs", None)
    if nexus_fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    search_service = _get_search_service(nexus_fs)

    op_context = get_operation_context(auth_result)
    permission_enforcer = getattr(request.app.state, "permission_enforcer", None)

    try:
        all_matches: list[str] = search_service.glob(
            pattern=pattern, path=path, context=op_context, files=files
        )
    except (ValueError, InvalidPathError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("glob failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"glob failed: {type(exc).__name__}") from exc

    # #3731: path_extractor=identity eliminates the _GlobResultShim shim class.
    pre_filter_count = len(all_matches)
    filtered_paths, filter_ms = _apply_rebac_filter(
        all_matches,
        permission_enforcer,
        auth_result,
        zone_id,
        path_extractor=lambda p: p,
    )
    post_filter_count = len(filtered_paths)

    total = len(filtered_paths)
    paginated = filtered_paths[offset : offset + limit]

    # Codex review of #3701 (review #2 finding #2 + review #3 finding #3):
    # unscope every glob path so the HTTP response surfaces user-facing
    # paths and never leaks the internal ``/zone/<tenant>/...`` form,
    # but also compute a parallel ``item_zones`` list so multi-zone
    # callers can distinguish colliding unscoped paths (e.g.
    # ``/zone/acme/src/x.py`` and ``/zone/beta/src/x.py`` both
    # unscope to ``/src/x.py`` — the zone id is the only round-trip
    # disambiguator). ``item_zones[i]`` is the zone of ``items[i]``
    # when one was recovered from the internal prefix, otherwise
    # ``None``.
    from nexus.core.path_utils import split_zone_from_internal_path

    item_zones: list[str | None] = []
    unscoped_items: list[str] = []
    for p in paginated:
        zone, unscoped = split_zone_from_internal_path(p)
        unscoped_items.append(unscoped)
        item_zones.append(zone)
    paginated = unscoped_items

    # Detect residual ambiguity (two results collapsing onto the same
    # (path, zone_id) after unscoping) and surface it in the envelope
    # so callers know round-trip safety is degraded.
    _keys = list(zip(paginated, item_zones, strict=False))
    glob_multi_zone_ambiguous = len(set(_keys)) < len(_keys)
    latency_ms = (time.perf_counter() - start_time) * 1000

    extras: dict[str, Any] = {
        "latency_ms": round(latency_ms, 2),
        "latency_breakdown": {
            "total_ms": round(latency_ms, 2),
            "permission_filter_ms": round(filter_ms, 2),
        },
        **_rebac_denial_stats(pre_filter_count, post_filter_count, limit + offset),
        # Codex review #3 finding #3: parallel zone disambiguation.
        # ``item_zones[i]`` is the zone id of ``items[i]`` (may be
        # ``None`` for root-zone paths). Multi-zone callers use this
        # to round-trip results back through ``files=[...]``.
        "item_zones": item_zones,
    }
    if glob_multi_zone_ambiguous:
        extras["multi_zone_ambiguous"] = True
    return build_paginated_list_response(
        items=paginated, total=total, offset=offset, limit=limit, extras=extras
    )


def _body_get_int(body: dict[str, Any], key: str, default: int, *, ge: int | None = None) -> int:
    """Extract an int from a JSON body with validation.

    Raises HTTPException(400) if the value is the wrong type or below
    a minimum bound. Used by POST handlers to validate body fields
    that would otherwise be validated by ``Query(..., ge=N)``.
    """
    raw = body.get(key, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise HTTPException(
            status_code=400, detail=f"Field {key!r} must be an int, got {type(raw).__name__}"
        )
    if ge is not None and raw < ge:
        raise HTTPException(status_code=400, detail=f"Field {key!r} must be >= {ge}, got {raw}")
    return raw


def _body_get_files(body: dict[str, Any]) -> list[str] | None:
    """Extract a ``files`` list from a JSON body with validation.

    ``None`` when absent (so the server walks the tree). ``[]`` is a
    legitimate empty-set short-circuit and is preserved. Non-list values
    or lists with non-string entries are 400s.
    """
    raw = body.get("files")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail=f"Field 'files' must be a list, got {type(raw).__name__}",
        )
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise HTTPException(
                status_code=400,
                detail=f"Field 'files[{i}]' must be a str, got {type(item).__name__}",
            )
    return raw


@router.get("/grep")
async def search_grep(
    request: Request,
    pattern: str = Query(..., description="Regex pattern to search for", min_length=1),
    path: str = Query("/", description="Base path to search from"),
    ignore_case: bool = Query(False, description="Case-insensitive match"),
    limit: int = Query(100, ge=1, le=10000, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Offset into the full result set"),
    before_context: int = Query(0, ge=0, le=50, description="Context lines before each match"),
    after_context: int = Query(0, ge=0, le=50, description="Context lines after each match"),
    invert_match: bool = Query(False, description="Return non-matching lines"),
    files: list[str] | None = Query(
        None,
        description=(
            "Optional stateless narrowing: restrict grep to this working "
            "set of file paths instead of walking the tree (#3701)."
        ),
    ),
    block_type: str | None = Query(
        None,
        description=(
            "Restrict matches to a specific markdown block type (#3720). "
            "Valid values: code, table, frontmatter, paragraph, "
            "blockquote, list, heading. Non-markdown files pass through "
            "unfiltered."
        ),
    ),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Search file contents via regex (#3701 Issue 1A).

    Mirrors the ``nexus_grep`` MCP tool but routes through the HTTP
    permission path (``_apply_rebac_filter``). Results are paginated via
    offset/limit and include ``permission_denial_rate`` /
    ``truncated_by_permissions`` when a permission enforcer is active.

    The ``files=[...]`` parameter (#3701 Issue 2A) lets agents pass a
    pre-narrowed working set so grep skips the tree walk. Repeat the
    query param for each path, e.g.
    ``?files=/src/a.py&files=/src/b.py``.

    **HTTP URL length limit**: very large file lists (typically >500–2000
    paths) can exceed the URL length limit of common HTTP clients. For
    those, use ``POST /api/v2/search/grep`` which accepts the same fields
    as a JSON body — no URL length constraint.
    """
    return await _do_grep_operation(
        request,
        auth_result,
        pattern=pattern,
        path=path,
        ignore_case=ignore_case,
        limit=limit,
        offset=offset,
        before_context=before_context,
        after_context=after_context,
        invert_match=invert_match,
        files=files,
        block_type=block_type,
    )


@router.post("/grep")
async def search_grep_post(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """POST variant of ``/api/v2/search/grep`` accepting a JSON body.

    Use this when the ``files=[...]`` working set is large enough to
    exceed the URL length limit of your HTTP client (typically >500–2000
    paths). The JSON body has no length constraint up to the 10,000-file
    ``FILES_FILTER_SIZE_CAP`` enforced server-side.

    Request body:

    .. code-block:: json

        {
            "pattern": "TODO",
            "path": "/workspace",
            "ignore_case": false,
            "limit": 100,
            "offset": 0,
            "before_context": 0,
            "after_context": 0,
            "invert_match": false,
            "files": ["/src/a.py", "/src/b.py", "..."]
        }

    Only ``pattern`` is required. All other fields default to the same
    values as the GET handler's ``Query(...)`` defaults.

    Response shape is identical to the GET handler.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    pattern = body.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise HTTPException(
            status_code=400, detail="Field 'pattern' is required and must be a non-empty string"
        )

    path = body.get("path", "/")
    if not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Field 'path' must be a string")

    ignore_case = bool(body.get("ignore_case", False))
    invert_match = bool(body.get("invert_match", False))
    limit = _body_get_int(body, "limit", 100, ge=1)
    if limit > 10000:
        raise HTTPException(status_code=400, detail="Field 'limit' must be <= 10000")
    offset = _body_get_int(body, "offset", 0, ge=0)
    before_context = _body_get_int(body, "before_context", 0, ge=0)
    after_context = _body_get_int(body, "after_context", 0, ge=0)
    if before_context > 50 or after_context > 50:
        raise HTTPException(status_code=400, detail="Context lines must be <= 50")

    files = _body_get_files(body)

    # Issue #3720: block_type (optional string, no type coercion needed).
    block_type = body.get("block_type")
    if block_type is not None and not isinstance(block_type, str):
        raise HTTPException(status_code=400, detail="Field 'block_type' must be a string")

    return await _do_grep_operation(
        request,
        auth_result,
        pattern=pattern,
        path=path,
        ignore_case=ignore_case,
        limit=limit,
        offset=offset,
        before_context=before_context,
        after_context=after_context,
        invert_match=invert_match,
        files=files,
        block_type=block_type,
    )


@router.get("/glob")
async def search_glob(
    request: Request,
    pattern: str = Query(..., description="Glob pattern (e.g. '**/*.py')", min_length=1),
    path: str = Query("/", description="Base path to search from"),
    limit: int = Query(100, ge=1, le=10000, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Offset into the full result set"),
    files: list[str] | None = Query(
        None,
        description=(
            "Optional stateless narrowing: match the glob pattern against "
            "this working set only instead of walking the tree (#3701)."
        ),
    ),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Search file paths via glob pattern (#3701 Issue 1A).

    Mirrors the ``nexus_glob`` MCP tool with HTTP-side ReBAC filtering.
    Supports the ``files=[...]`` stateless narrowing parameter.

    For very large ``files=[...]`` working sets that exceed the URL
    length limit of your HTTP client (typically >500–2000 paths), use
    ``POST /api/v2/search/glob`` with a JSON body.
    """
    return await _do_glob_operation(
        request,
        auth_result,
        pattern=pattern,
        path=path,
        limit=limit,
        offset=offset,
        files=files,
    )


@router.post("/glob")
async def search_glob_post(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """POST variant of ``/api/v2/search/glob`` accepting a JSON body.

    Use this when the ``files=[...]`` working set is large enough to
    exceed the URL length limit of your HTTP client (typically >500–2000
    paths). The JSON body has no length constraint up to the 10,000-file
    ``FILES_FILTER_SIZE_CAP`` enforced server-side.

    Request body:

    .. code-block:: json

        {
            "pattern": "**/*.py",
            "path": "/workspace",
            "limit": 100,
            "offset": 0,
            "files": ["/src/a.py", "/src/b.py", "..."]
        }

    Only ``pattern`` is required. Response shape is identical to the GET
    handler.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    pattern = body.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise HTTPException(
            status_code=400, detail="Field 'pattern' is required and must be a non-empty string"
        )

    path = body.get("path", "/")
    if not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Field 'path' must be a string")

    limit = _body_get_int(body, "limit", 100, ge=1)
    if limit > 10000:
        raise HTTPException(status_code=400, detail="Field 'limit' must be <= 10000")
    offset = _body_get_int(body, "offset", 0, ge=0)
    files = _body_get_files(body)

    return await _do_glob_operation(
        request,
        auth_result,
        pattern=pattern,
        path=path,
        limit=limit,
        offset=offset,
        files=files,
    )


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
    - Duplicate registration: **idempotent recovery**. Instead of 409,
      we re-trigger the backfill so an operator who hit a previous
      backfill failure can retry by re-issuing the same POST. The
      response includes ``status: "already_registered"`` so the
      caller can distinguish from a fresh registration.

    Backfill outcomes (``backfill_status``):
    - ``ok``: backfill ran cleanly (``backfill_files`` may be 0 if
      the zone genuinely has no in-scope chunks yet).
    - ``skewed``: a concurrent scope mutation superseded this
      backfill. Response includes ``degraded: true``; operator should
      retry by re-issuing this POST.
    - ``failed``: hard failure reading or writing txtai. Metadata
      change is committed; response includes ``degraded: true`` and
      the error message. Operator can retry by re-issuing.
    - ``no_op``: daemon has no DB or backend (test scaffolding).
    """
    from nexus.bricks.search.index_scope import (
        DirectoryAlreadyRegisteredError,
        InvalidDirectoryPathError,
        ZoneNotFoundError,
    )
    from nexus.bricks.search.scope_ops import BackfillFailedError
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    directory_path = payload.get("path")
    if not isinstance(directory_path, str) or not directory_path:
        raise HTTPException(status_code=400, detail="'path' field is required")

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    await _require_admin_or_path_write(request, auth_result, zone_id, directory_path)

    canonical = directory_path
    status_label = "registered"
    backfill_status = "ok"
    backfill_files = 0
    backfill_error: str | None = None
    backfill_attempted = 0

    try:
        canonical, result = await search_daemon.add_indexed_directory(zone_id, directory_path)
        backfill_status = result.status
        backfill_files = result.files
    except ZoneNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DirectoryAlreadyRegisteredError:
        # **Idempotent retry**: instead of 409, re-trigger backfill so
        # an operator who hit an earlier backfill failure can recover
        # by re-issuing the same POST. Mark the response so the caller
        # can distinguish from a fresh registration.
        from nexus.bricks.search.scope_ops import validate_directory_path

        canonical = validate_directory_path(directory_path)
        status_label = "already_registered"
        try:
            result = await search_daemon.rerun_backfill_for_directory(zone_id, directory_path)
            backfill_status = result.status
            backfill_files = result.files
        except BackfillFailedError as exc:
            backfill_status = "failed"
            backfill_error = str(exc)
            backfill_attempted = exc.files_attempted
            logger.warning(
                "rerun backfill for already-registered %s failed: %s",
                canonical,
                exc,
            )
    except BackfillFailedError as exc:
        # The metadata change committed BUT the backfill failed.
        # Recompute canonical from the input since the exception
        # doesn't carry it.
        from nexus.bricks.search.scope_ops import validate_directory_path

        try:
            canonical = validate_directory_path(directory_path)
        except Exception:
            canonical = directory_path
        backfill_status = "failed"
        backfill_error = str(exc)
        backfill_attempted = exc.files_attempted
        logger.warning(
            "register /index-directory %s succeeded but backfill failed: %s",
            canonical,
            exc,
        )

    response: dict[str, Any] = {
        "zone_id": zone_id,
        "path": canonical,
        "status": status_label,
        "backfill_status": backfill_status,
        "backfill_files": backfill_files,
    }
    if backfill_status == "failed":
        response["backfill_error"] = backfill_error
        response["backfill_attempted"] = backfill_attempted
        response["degraded"] = True
    elif backfill_status == "skewed":
        # Concurrent mutation superseded this backfill. The metadata
        # change is committed but historical content was not
        # backfilled. Operator should retry by re-issuing this POST.
        response["degraded"] = True
        response["backfill_hint"] = (
            "concurrent scope mutation superseded the backfill; re-issue this POST to retry"
        )
    return response


@router.delete("/index-directory")
async def unregister_indexed_directory(
    request: Request,
    payload: dict[str, Any],
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Unregister a directory from scoped semantic indexing (Issue #3698).

    Returns 404 if the directory was not registered.

    After successful unregistration this endpoint runs
    ``purge_unscoped_embeddings`` for the zone so any txtai rows that
    were under the removed directory disappear from semantic search at
    the same instant. The canonical ``document_chunks`` rows are
    preserved (purge only touches derived txtai state) so a future
    re-registration or mode flip back to ``'all'`` can rebuild the
    semantic index from the existing chunk store.
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

    # Auto-purge stale derived txtai rows so query results stop showing
    # the now-unscoped files immediately. The metadata change is the
    # source of truth and is already committed.
    #
    # **Fail-closed at the HTTP boundary**: if purge raises, return
    # 503 Service Unavailable so clients keying off HTTP status know
    # the request did NOT fully succeed and stale txtai data may
    # still be searchable. Returning 200 with ``degraded=true`` was
    # misleading — automation that only inspects status codes would
    # treat the de-scope as complete while txtai still served the
    # old data, which is a real data-exposure risk at this trust
    # boundary. The metadata change is NOT rolled back: the periodic
    # _scope_refresh_loop will retry the purge on its next tick, and
    # operators can also retry via /purge-unscoped explicitly.
    try:
        purged = await search_daemon.purge_unscoped_embeddings(zone_id)
    except Exception as exc:
        logger.warning(
            "auto-purge after unregister failed for zone %s; "
            "metadata committed, returning 503 so the caller retries",
            zone_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "purge_failed",
                "message": str(exc),
                "zone_id": zone_id,
                "path": canonical,
                "metadata_committed": True,
                "retry_via": "/api/v2/search/purge-unscoped",
            },
        ) from exc

    return {
        "zone_id": zone_id,
        "path": canonical,
        "status": "unregistered",
        "purged": purged,
        "purge_status": "ok",
    }


@router.get("/indexed-dirs")
async def list_indexed_dirs(
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """List directories currently registered for scoped semantic indexing.

    **Admin-only**: returning the registered directory list verbatim
    leaks the prefix layout (e.g., customer / repo / project names
    embedded in paths) to anyone who can authenticate against the
    zone, even if they have no read permission on the prefixes
    themselves. The mutation endpoints already require admin or
    explicit ReBAC write; this read should match.

    Returns an empty list if no directories are registered for the
    zone (which, combined with zone mode 'all', means the zone
    indexes everything).
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="indexed-dirs is admin-only (registered directory "
            "names can encode sensitive metadata)",
        )

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

    When flipping a zone from ``'all'`` to ``'scoped'``, this endpoint
    also runs ``purge_unscoped_embeddings`` for that zone so previously
    embedded out-of-scope files become invisible to semantic search at
    the same instant as the metadata change. Without that, the API
    would claim "takes effect immediately" while stale txtai rows
    remained searchable until a separate ``/purge-unscoped`` call ran.

    Request body:
        {"mode": "all" | "scoped", "zone_id": "optional — defaults to caller's zone"}
    """
    from nexus.bricks.search.index_scope import (
        INDEX_MODE_ALL,
        INDEX_MODE_SCOPED,
        InvalidDirectoryPathError,
        ZoneNotFoundError,
    )
    from nexus.bricks.search.scope_ops import BackfillFailedError
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(status_code=403, detail="set-indexing-mode is admin-only")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    mode = payload.get("mode")
    if not isinstance(mode, str) or not mode:
        raise HTTPException(status_code=400, detail="'mode' field is required")

    zone_id = payload.get("zone_id") or auth_result.get("zone_id") or ROOT_ZONE_ID

    backfill_status = "ok"
    backfill_files = 0
    backfill_error: str | None = None
    backfill_attempted = 0
    try:
        result = await search_daemon.set_zone_indexing_mode(zone_id, mode)
        if result is not None:
            backfill_status = result.status
            backfill_files = result.files
    except ZoneNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDirectoryPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BackfillFailedError as exc:
        # The mode flip from 'scoped' to 'all' committed BUT the
        # backfill of historical content failed. The metadata change
        # is irreversible (already persisted) so we surface a
        # degraded-state response. The next write or daemon restart
        # will pick up where backfill left off.
        backfill_status = "failed"
        backfill_error = str(exc)
        backfill_attempted = exc.files_attempted
        logger.warning(
            "indexing-mode flip on zone %s succeeded but backfill failed: %s",
            zone_id,
            exc,
        )

    purged: dict[str, int] | None = None
    if mode == INDEX_MODE_SCOPED:
        # Auto-purge stale derived rows so de-scoping is enforced
        # immediately at query time, not just at the next write.
        #
        # **Fail-closed at the HTTP boundary**: if purge raises,
        # return 503 so clients keying off HTTP status know the
        # request did NOT fully succeed and stale txtai data may
        # still be searchable. Returning 200 with ``degraded=true``
        # was misleading at this trust boundary. The metadata change
        # is NOT rolled back: the periodic _scope_refresh_loop will
        # retry the purge on its next tick, and operators can also
        # retry via /purge-unscoped explicitly.
        try:
            purged = await search_daemon.purge_unscoped_embeddings(zone_id)
        except Exception as exc:
            logger.warning(
                "auto-purge after mode=scoped flip failed for zone %s; "
                "metadata committed, returning 503 so the caller retries",
                zone_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "purge_failed",
                    "message": str(exc),
                    "zone_id": zone_id,
                    "indexing_mode": mode,
                    "metadata_committed": True,
                    "retry_via": "/api/v2/search/purge-unscoped",
                },
            ) from exc

    response: dict[str, Any] = {
        "zone_id": zone_id,
        "indexing_mode": mode,
        "status": "updated",
    }
    if mode == INDEX_MODE_ALL:
        response["backfill_status"] = backfill_status
        response["backfill_files"] = backfill_files
        if backfill_status == "failed":
            response["backfill_error"] = backfill_error
            response["backfill_attempted"] = backfill_attempted
            response["degraded"] = True
        elif backfill_status == "skewed":
            response["degraded"] = True
            response["backfill_hint"] = (
                "concurrent scope mutation superseded the backfill; re-issue this POST to retry"
            )
    if mode == INDEX_MODE_SCOPED:
        response["purge_status"] = "ok"
        if purged is not None:
            response["purged"] = purged
    return response


@router.post("/purge-unscoped")
async def purge_unscoped_embeddings(
    payload: dict[str, Any] | None = None,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Admin-only: purge derived embedding artifacts for files outside any scope.

    Destructive operation (Issue #3698). Call this after unregistering
    directories to clean up stale txtai sections/vectors for files that
    are no longer in scope. Only zones in ``'scoped'`` mode are affected.

    Only deletes **derived** txtai state (``sections``, ``vectors``,
    in-memory index). The canonical ``document_chunks`` table is
    preserved so a future mode-flip back to ``'all'`` can rebuild
    semantic search from existing chunks.

    Request body (optional):
        {"zone_id": "zone-name"}    # defaults to caller's zone

    Both this endpoint and ``/indexing-mode`` accept the same payload
    shape so an admin operating across zones doesn't accidentally
    purge the wrong one.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    if not auth_result.get("is_admin", False):
        raise HTTPException(
            status_code=403,
            detail="purge-unscoped is admin-only",
        )

    body: dict[str, Any] = payload or {}
    zone_id = body.get("zone_id") or auth_result.get("zone_id") or ROOT_ZONE_ID
    counts = await search_daemon.purge_unscoped_embeddings(zone_id)
    return {
        "zone_id": zone_id,
        "purged": counts,
    }


# =============================================================================
# /locate — global path+title skeleton search (Issue #3725)
# =============================================================================


class _LocateRequest(dict):
    """Pydantic-free request body model for /locate.

    Using a plain dict subclass keeps the endpoint self-contained and avoids
    adding a Pydantic model for a single struct.  Field validation is inline.
    """


@router.post("/locate")
async def search_locate(
    request: Request,
    payload: dict[str, Any] | None = None,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Fast global path+title file locator (Issue #3725).

    Returns ranked candidate file paths whose path tokens or extracted title
    match the query.  No embeddings, no LLM — BM25-lite over path+title.

    Request body (JSON):
        q          (str, required)   Natural-language or keyword query.
        zone_id    (str, optional)   Zone to search; defaults to caller's zone.
        limit      (int, optional)   Max candidates, 1–100, default 20.
        path_prefix (str, optional)  Restrict results to this path prefix.

    Response:
        {
          "candidates": [
            {"path": "/workspace/src/auth/login.py", "score": 8.4, "title": "..."},
            ...
          ],
          "total_before_filter": <int>,
          "permission_denial_rate": <float>,
          "truncated_by_permissions": <bool>,
          "elapsed_ms": <float>
        }
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    start_time = time.perf_counter()

    if not search_daemon.is_initialized:
        raise HTTPException(status_code=503, detail="Search daemon is still initializing")

    if not hasattr(search_daemon, "locate"):
        raise HTTPException(
            status_code=501, detail="Skeleton index not available on this daemon version"
        )

    body: dict[str, Any] = payload or {}
    q: str = body.get("q", "")
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="'q' is required and must be non-empty")

    caller_zone = auth_result.get("zone_id") or ROOT_ZONE_ID
    zone_id: str = body.get("zone_id") or caller_zone
    path_prefix: str | None = body.get("path_prefix")

    raw_limit = body.get("limit", 20)
    try:
        effective_limit = max(1, min(100, int(raw_limit)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'limit' must be an integer 1–100") from None

    # Over-fetch for ReBAC filtering (follows _compute_rebac_fetch_limit pattern)
    permission_enforcer = getattr(request.app.state, "permission_enforcer", None)
    fetch_limit = _compute_rebac_fetch_limit(
        effective_limit, has_enforcer=permission_enforcer is not None
    )

    # --- Query skeleton index ---
    raw_candidates = await search_daemon.locate(
        q,
        zone_id=zone_id,
        limit=fetch_limit,
        path_prefix=path_prefix,
    )
    total_before_filter = len(raw_candidates)

    # --- ReBAC filtering (11A) ---
    # Adapt candidates to the shape _apply_rebac_filter expects (needs .path attr)
    class _PathResult:
        def __init__(self, d: dict[str, Any]) -> None:
            self.path = d["path"]
            self._d = d

    result_objects = [_PathResult(c) for c in raw_candidates]
    filtered_objects, filter_ms = _apply_rebac_filter(
        result_objects,
        permission_enforcer,
        auth_result,
        zone_id,
    )

    # Unpack back to dicts and truncate to effective_limit
    candidates = [obj._d for obj in filtered_objects[:effective_limit]]

    denial_stats = _rebac_denial_stats(total_before_filter, len(filtered_objects), effective_limit)
    elapsed_ms = (time.perf_counter() - start_time) * 1000

    logger.debug(
        "[LOCATE] q=%r zone=%s → %d/%d candidates in %.1fms (filter %.1fms)",
        q,
        zone_id,
        len(candidates),
        total_before_filter,
        elapsed_ms,
        filter_ms,
    )

    return {
        "candidates": candidates,
        "total_before_filter": total_before_filter,
        "elapsed_ms": round(elapsed_ms, 2),
        **denial_stats,
    }
