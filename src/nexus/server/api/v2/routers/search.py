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
- GET  /api/v2/search/parked            -- parked (poison) mutation events (#4337, admin)
- POST /api/v2/search/parked/retry      -- re-drive parked events (#4337, admin)
- POST /api/v2/search/parked/discard    -- discard parked events (#4337, admin)
- POST /api/v2/search/consumers/{name}/skip-to -- force checkpoint advance (#4337, admin)

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

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from nexus.bricks.search.daemon import _BACKEND_LEG_TIMING_KEYS
from nexus.bricks.search.graph_search_service import GraphBackendUnavailable
from nexus.lib.pagination import build_paginated_list_response
from nexus.lib.rebac_filter import apply_rebac_filter as _apply_rebac_filter
from nexus.lib.rebac_filter import compute_rebac_fetch_limit as _compute_rebac_fetch_limit
from nexus.lib.rebac_filter import rebac_denial_stats as _rebac_denial_stats
from nexus.runtime.zone_resolution import target_zone_for_context
from nexus.server.dependencies import get_operation_context, require_admin, require_auth
from nexus.server.zone_execution import run_zone_scoped

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


# _get_search_daemon lives in _search_deps.py so the split sub-routers
# (_search_indexed_dirs, _search_locate) can import it without forming
# a cycle back through search.py.  Re-export here for the many callers
# / tests / monkeypatches that already reach it via `search._get_search_daemon`.
from nexus.server.api.v2.routers._search_deps import _get_search_daemon  # noqa: E402


def _get_record_store(request: Request) -> Any:
    """Get RecordStore from app.state."""
    store = getattr(request.app.state, "record_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Record store not available")
    return store


def _add_backend_leg_timings(
    latency_breakdown: dict[str, float],
    daemon_timing: Any,
) -> None:
    if not isinstance(daemon_timing, dict):
        return
    for key in _BACKEND_LEG_TIMING_KEYS:
        value = daemon_timing.get(key)
        if isinstance(value, int | float):
            latency_breakdown[key] = round(float(value), 2)


def _bind_search_phase_timings(latency_breakdown: dict[str, float]) -> None:
    """Bind per-query phase timings onto the structlog request context (#4269).

    The CorrelationMiddleware emits ``request_completed`` with every bound
    contextvar, so prefixing each ``latency_breakdown`` leg with ``search_``
    and binding it here makes the phase split (backend / keyword / page /
    vector / fusion / index-load / permission-filter) queryable per request
    in log aggregators — the evidence the issue asks for to pin I/O vs compute.
    """
    bound = {
        f"search_{key}": round(float(value), 2)
        for key, value in latency_breakdown.items()
        if isinstance(value, int | float)
    }
    if bound:
        structlog.contextvars.bind_contextvars(**bound)


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


def _get_zone_registry(request: Request) -> Any | None:
    return getattr(request.app.state, "zone_registry", None)


def _auth_target_zone(auth_result: dict[str, Any]) -> str | None:
    from nexus.contracts.constants import ROOT_ZONE_ID

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    return zone_id if zone_id != ROOT_ZONE_ID else None


# ReBAC filtering helpers are in nexus.lib.rebac_filter (#3731).

# Response shaping lives in ``_search_serialize`` (#3701 Issue 5A; extracted
# at the 2000-line router cap during the #4545 rebase). Re-exported here so
# existing ``from ...routers.search import _serialize_search_result`` imports
# keep working.
from nexus.server.api.v2.routers._search_serialize import (  # noqa: E402
    _serialize_search_result,
)

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


class ParkedRetryRequest(BaseModel):
    consumer: str
    event_ids: list[str] | None = None


class ParkedDiscardRequest(BaseModel):
    consumer: str
    event_ids: list[str]


class ConsumerSkipToRequest(BaseModel):
    sequence: int


@router.get("/parked")
async def search_parked_list(
    _admin: dict[str, Any] = Depends(require_admin),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """List parked (poison) mutation events per consumer (#4337)."""
    return {"parked": search_daemon.list_parked()}


@router.post("/parked/retry")
async def search_parked_retry(
    body: ParkedRetryRequest,
    _admin: dict[str, Any] = Depends(require_admin),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Re-drive parked mutation events through their consumer (#4337)."""
    try:
        result: dict[str, Any] = await search_daemon.retry_parked(body.consumer, body.event_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Parked-event retry failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Parked-event retry failed") from exc
    return result


@router.post("/parked/discard")
async def search_parked_discard(
    body: ParkedDiscardRequest,
    _admin: dict[str, Any] = Depends(require_admin),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Discard parked mutation events without retrying (#4337)."""
    try:
        result: dict[str, Any] = await search_daemon.discard_parked(body.consumer, body.event_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Parked-event discard failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Parked-event discard failed") from exc
    return result


@router.post("/consumers/{consumer_name}/skip-to")
async def search_consumer_skip_to(
    consumer_name: str,
    body: ConsumerSkipToRequest,
    _admin: dict[str, Any] = Depends(require_admin),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Force-advance a mutation consumer checkpoint past a poisoned range (#4337)."""
    try:
        result: dict[str, int] = await search_daemon.force_checkpoint(consumer_name, body.sequence)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Checkpoint skip-to failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Checkpoint skip-to failed") from exc
    return result


@router.get("/query")
async def search_query(
    request: Request,
    q: str = Query(..., description="Search query text", min_length=1),
    type: str = Query("hybrid", description="Search type: keyword, semantic, or hybrid"),
    limit: int = Query(10, description="Maximum number of results", ge=1, le=100),
    path: str | None = Query(None, description="Optional path prefix filter"),
    alpha: float = Query(0.5, description="Semantic vs keyword weight (0.0-1.0)", ge=0.0, le=1.0),
    fusion: str = Query("rrf", description="Fusion method: rrf, weighted, or rrf_weighted"),
    rrf_k: int = Query(60, description="RRF rank constant for hybrid fusion", ge=1, le=1000),
    expand: str = Query("none", description="Context expansion: none or macro"),
    recency: str | None = Query(
        None,
        description="Recency boost mode: off, on, or auto (default: server config; applies to non-graph search — local zones only in federated mode, #4543)",
    ),
    recency_weight: float | None = Query(
        None,
        description="Recency boost weight w in score*=1+w*H/(H+age_days) (default: server config)",
        ge=0.0,
        le=5.0,
    ),
    recency_half_life_days: float | None = Query(
        None,
        description="Recency half-life H in days (default: server config)",
        gt=0.0,
        le=3650.0,
    ),
    rerank: bool | None = Query(  # noqa: ARG001
        None,
        description="Inert: accepted for compatibility; no reranker stage exists (#4541)",
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
    from nexus.contracts.constants import ROOT_ZONE_ID

    start_time = time.perf_counter()
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID

    # Raw credential scope vs synthesized routing scope: an EMPTY zone_set is
    # the auth contract for unconstrained credentials (admin/internal keys) —
    # only the synthesized fallback below uses the active zone_id, and that
    # must never be treated as a token allow-list (#4541 review round 5).
    raw_zone_set = tuple(auth_result.get("zone_set") or ())
    zone_set = raw_zone_set or (zone_id,)
    # #4542 rounds 8-10: readable allow-list (None = admin/root/unbounded).
    from nexus.bricks.search.federated_search import token_zone_filter_from_auth

    token_zone_filter = token_zone_filter_from_auth(auth_result, root_zone_id=ROOT_ZONE_ID)
    # #3785: auto-promote to federated when token grants multiple zones,
    # even if caller didn't pass federated=true. Single-zone tokens
    # (zone_set == (zone_id,)) hit the unchanged single-zone path.
    if len(zone_set) > 1:
        federated = True

    # Issue #4542 round-9: read attenuation on BOTH routes — federated via
    # zone_filter, single-zone checked here; no-readable-zone fails closed.
    if token_zone_filter is not None:
        if not token_zone_filter:
            raise HTTPException(403, "Token has no zone with read permission for search")
        if not federated and zone_id not in token_zone_filter:
            raise HTTPException(403, f"Token has no read permission for zone {zone_id}")

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

    if expand not in ("none", "macro"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid expand: {expand}. Must be 'none' or 'macro'",
        )

    if recency is not None and recency not in ("off", "on", "auto"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid recency: {recency}. Must be 'off', 'on', or 'auto'",
        )

    if graph_mode not in ("none", "low", "high", "dual", "auto"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid graph_mode: {graph_mode}. Must be 'none', 'low', 'high', 'dual', or 'auto'",
        )

    target_zone = zone_id if zone_id != ROOT_ZONE_ID else None

    # Token zone allow-list for the federated path (#3785). An EXPLICIT
    # singleton non-root zone_set must scope federation too: without it, a
    # single-zone token requesting federated=true reached the dispatcher with
    # no filter and searched every subject-accessible zone, bypassing the
    # token's allow-list (#4541 review round 3). Unrestricted stays None for:
    # an empty raw scope (unconstrained/admin credentials — the synthesized
    # zone_id fallback is routing metadata, not an allow-list) and a scope of
    # exactly {root} (root grants cross-zone access and its id never
    # intersects concrete zone names). A multi-zone scope that happens to
    # include root keeps its pre-existing filtered behaviour.
    async def _work() -> dict[str, Any]:
        # --- Federated search path (Issue #3147) ---
        # NOTE: expand= is single-zone only; federated path does not support it.
        if federated:
            return await _handle_federated_search(
                q=q,
                search_type=type,
                limit=limit,
                path_filter=path,
                alpha=alpha,
                fusion_method=fusion,
                rrf_k=rrf_k,
                recency=recency,
                recency_weight=recency_weight,
                recency_half_life_days=recency_half_life_days,
                auth_result=auth_result,
                search_daemon=search_daemon,
                request=request,
                zone_filter=token_zone_filter,
            )

        return await _handle_single_zone_search(
            request=request,
            q=q,
            search_type=type,
            limit=limit,
            path_filter=path,
            alpha=alpha,
            fusion_method=fusion,
            rrf_k=rrf_k,
            graph_mode=graph_mode,
            expand=expand,
            recency=recency,
            recency_weight=recency_weight,
            recency_half_life_days=recency_half_life_days,
            auth_result=auth_result,
            search_daemon=search_daemon,
            async_session_factory=async_session_factory,
            record_store=record_store,
            zone_id=zone_id,
            start_time=start_time,
        )

    return await run_zone_scoped(_get_zone_registry(request), target_zone, _work)


async def _handle_single_zone_search(
    *,
    request: Request,
    q: str,
    search_type: str,
    limit: int,
    path_filter: str | None,
    alpha: float,
    fusion_method: str,
    rrf_k: int,
    recency: str | None = None,
    recency_weight: float | None = None,
    recency_half_life_days: float | None = None,
    graph_mode: str,
    expand: str,
    auth_result: dict[str, Any],
    search_daemon: Any,
    async_session_factory: Any,
    record_store: Any,
    zone_id: str,
    start_time: float,
) -> dict[str, Any]:
    """Handle the non-federated search branch."""
    from nexus.bricks.search.query_router import QueryRouter

    # --- Standard single-zone search path ---
    # ReBAC file-level permission enforcer (Decision #17)
    permission_enforcer = getattr(request.app.state, "permission_enforcer", None)
    op_context = get_operation_context(auth_result)

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

            graph_fell_back = False
            results: list[Any] = []
            try:
                results = await graph_enhanced_search(
                    query=q,
                    search_type=search_type,
                    limit=fetch_limit,
                    path_filter=path_filter,
                    alpha=alpha,
                    graph_mode=effective_graph_mode,
                    record_store=record_store,
                    async_session_factory=async_session_factory,
                    search_daemon=search_daemon,
                    zone_id=zone_id,
                )
            except GraphBackendUnavailable:
                # Fail-open: graph backend was removed in #3699 and the
                # NEXUS_TXTAI_GRAPH knob is vestigial.  Fall through to
                # normal search instead of returning zero results.
                logger.warning(
                    "graph_mode=%s requested but graph backend unavailable; "
                    "falling back to normal search (txtai graph removed in #3699)",
                    effective_graph_mode,
                )
                effective_graph_mode = "none"
                graph_fell_back = True

            if not graph_fell_back:
                # ReBAC file-level filtering (Decision #17)
                pre_filter_count = len(results)
                results, filter_ms = _apply_rebac_filter(
                    results,
                    permission_enforcer,
                    auth_result,
                    zone_id,
                    operation_context=op_context,
                )
                post_filter_count = len(results)
                results = results[:effective_limit]

                latency_ms = (time.perf_counter() - start_time) * 1000

                graph_latency_breakdown = {
                    "total_ms": round(latency_ms, 2),
                    "permission_filter_ms": round(filter_ms, 2),
                }
                _bind_search_phase_timings(graph_latency_breakdown)

                response: dict[str, Any] = {
                    "query": q,
                    "search_type": search_type,
                    "graph_mode": effective_graph_mode,
                    "results": [_serialize_search_result(r) for r in results],
                    "total": len(results),
                    "latency_ms": round(latency_ms, 2),
                    "latency_breakdown": graph_latency_breakdown,
                    **_rebac_denial_stats(pre_filter_count, post_filter_count, effective_limit),
                }
                if routing_info:
                    response["routing"] = routing_info
                return response
            # else: graph fell back to normal search — fall through below

        results = await search_daemon.search(
            query=q,
            search_type=search_type,
            limit=fetch_limit,
            path_filter=path_filter,
            alpha=alpha,
            fusion_method=fusion_method,
            rrf_k=rrf_k,
            zone_id=zone_id,
            expand=expand,
            recency=recency,
            recency_weight=recency_weight,
            recency_half_life_days=recency_half_life_days,
        )

        # Prefer the request-local snapshot carried by SearchDaemon results.
        # Fall back to the legacy daemon field for older/mocked search daemons.
        daemon_timing = getattr(results, "search_timing", None)
        if daemon_timing is None:
            daemon_timing = getattr(search_daemon, "last_search_timing", {})
        backend_ms = daemon_timing.get("backend_ms", 0.0)
        rerank_ms = daemon_timing.get("rerank_ms", 0.0)

        # Capture request-level degradation BEFORE the ReBAC filter — it
        # returns a plain list, dropping the SearchResultList flag. The
        # list-level flag matters for EMPTY degraded responses, where no
        # per-result marker exists (#4541 review round 8).
        semantic_degraded_flag = bool(getattr(results, "semantic_degraded", False)) or any(
            getattr(r, "semantic_degraded", None) for r in results
        )

        # ReBAC file-level filtering (Decision #17)
        pre_filter_count = len(results)
        results, filter_ms = _apply_rebac_filter(
            results,
            permission_enforcer,
            auth_result,
            zone_id,
            operation_context=op_context,
        )
        post_filter_count = len(results)
        results = results[:effective_limit]

        latency_ms = (time.perf_counter() - start_time) * 1000

        latency_breakdown = {
            "total_ms": round(latency_ms, 2),
            "backend_ms": round(backend_ms, 2),
            "rerank_ms": round(rerank_ms, 2),
            "permission_filter_ms": round(filter_ms, 2),
        }
        _add_backend_leg_timings(latency_breakdown, daemon_timing)
        _bind_search_phase_timings(latency_breakdown)

        response = {
            "query": q,
            "search_type": search_type,
            "graph_mode": "none",
            "results": [_serialize_search_result(r) for r in results],
            "total": len(results),
            "latency_ms": round(latency_ms, 2),
            "latency_breakdown": latency_breakdown,
            **_rebac_denial_stats(pre_filter_count, post_filter_count, effective_limit),
        }
        # Truthy-only so default responses stay byte-identical (#3778 shape).
        if semantic_degraded_flag:
            response["semantic_degraded"] = True
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
    rrf_k: int,
    recency: str | None = None,
    recency_weight: float | None = None,
    recency_half_life_days: float | None = None,
    auth_result: dict[str, Any],
    search_daemon: Any,
    request: Request,
    zone_filter: frozenset[str] | None = None,  # NEW (#3785)
) -> dict[str, Any]:
    """Handle federated cross-zone search (Issue #3147).

    Delegates to FederatedSearchDispatcher which fans out search
    across all accessible zones and fuses results via raw score merge.

    Issue #3778: when the active deployment profile is SANDBOX and every
    federated peer reports unreachable, delegates to
    ``SearchService._semantic_with_sandbox_fallback`` so the response
    surfaces BM25S results stamped with ``semantic_degraded=True``.
    """
    from nexus.bricks.search.federated_search import (
        FederatedSearchDispatcher,
        is_all_peers_failed,
    )

    # Issue #4269 (Codex R3): the SANDBOX BM25S fallback below runs AFTER the
    # dispatcher returns and is not in fed_response.latency_ms, so track its
    # time separately and fold it into the reported total.
    fed_fallback_ms = 0.0

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
        rrf_k=rrf_k,
        recency=recency,
        recency_weight=recency_weight,
        recency_half_life_days=recency_half_life_days,
        zone_filter=zone_filter,
    )

    # Issue #3778: SANDBOX profile — degrade semantic federation to local
    # BM25S when every peer is unreachable.  Stamp results with
    # ``semantic_degraded=True`` so callers can distinguish degraded pages.
    semantic_degraded = False
    profile = (getattr(request.app.state, "deployment_profile", "") or "").lower()
    if (
        profile == "sandbox"
        and search_type in ("semantic", "hybrid")
        and is_all_peers_failed(fed_response)
    ):
        nexus_fs = getattr(request.app.state, "nexus_fs", None)
        search_service = None
        if nexus_fs is not None:
            try:
                search_service = nexus_fs.service("search")
            except Exception:
                search_service = None

        if search_service is not None:
            from nexus.server.dependencies import get_operation_context

            op_context = get_operation_context(auth_result)
            # Issue #4542 round-6: this all-peers-failed fallback replaces the
            # dispatcher's capped results wholesale, so it must honor the
            # per-document cap itself — fetch wider, cap, trim.
            from nexus.bricks.search.federated_search import daemon_pooling_cap

            _fb_cap = daemon_pooling_cap(search_daemon)
            fallback_start = time.perf_counter()
            bm25s_results = await search_service.semantic_search(
                query=q,
                path=path_filter or "/",
                limit=limit if _fb_cap is None else limit * 2,
                search_mode="semantic",  # triggers SANDBOX fallback inside SearchService
                context=op_context,
            )
            if _fb_cap is not None:
                from nexus.bricks.search.result_builders import cap_chunks_per_page

                bm25s_results = cap_chunks_per_page(list(bm25s_results), chunks_per_page=_fb_cap)[
                    :limit
                ]
            # Record the degraded-path BM25S fallback work so the bound
            # total_ms / fallback_ms reflect it (Codex R3).
            fed_fallback_ms = (time.perf_counter() - fallback_start) * 1000
            # semantic_search stamped semantic_degraded=True on each dict
            # AND sets LAST_SEMANTIC_DEGRADED for this task — we prefer the
            # contextvar so an empty BM25S result still surfaces degradation
            # (R2 review).
            fed_response.results = list(bm25s_results)
            from nexus.contracts.search_types import LAST_SEMANTIC_DEGRADED

            semantic_degraded = LAST_SEMANTIC_DEGRADED.get() or any(
                isinstance(r, dict) and r.get("semantic_degraded") is True for r in bm25s_results
            )

    # Issue #4269 (Codex R2): /search/query auto-promotes multi-zone tokens
    # into this federated path, so without binding here those request_completed
    # logs would omit even search_total_ms — an observability blind spot for
    # cross-zone searches. total_ms = dispatcher latency + the SANDBOX BM25S
    # fallback (Codex R3), which runs after the dispatcher returns and is not in
    # fed_response.latency_ms. (Per-leg backend timings are not aggregated
    # across zones by the dispatcher.)
    fed_total_ms = fed_response.latency_ms + fed_fallback_ms
    fed_latency_breakdown = {"total_ms": round(fed_total_ms, 2)}
    # Per-leg backend timings aggregated across local zones (Codex R6): surface
    # index_load_ms / keyword_ms / vector_ms / fusion_ms so a cold federated
    # query exposes the same phase split as a single-zone one.
    for key, value in (getattr(fed_response, "search_timing", None) or {}).items():
        if isinstance(value, int | float):
            fed_latency_breakdown[key] = round(float(value), 2)
    if fed_fallback_ms:
        fed_latency_breakdown["fallback_ms"] = round(
            fed_latency_breakdown.get("fallback_ms", 0.0) + fed_fallback_ms, 2
        )
    _bind_search_phase_timings(fed_latency_breakdown)

    response_dict: dict[str, Any] = {
        "query": q,
        "search_type": search_type,
        "graph_mode": "none",
        "federated": True,
        "results": fed_response.results,
        "total": len(fed_response.results),
        "latency_ms": round(fed_total_ms, 2),
        "latency_breakdown": fed_latency_breakdown,
        "zones_searched": fed_response.zones_searched,
        "zones_failed": [
            {"zone_id": zf.zone_id, "error": zf.error} for zf in fed_response.zones_failed
        ],
    }
    if fed_response.zones_skipped:
        response_dict["zones_skipped"] = fed_response.zones_skipped
    if fed_response.cached:
        response_dict["cached"] = True
    # Either the #3778 sandbox BM25S fallback (local flag) or a zone-level
    # degraded dense leg reported by the dispatcher (#4541 review round 9).
    if semantic_degraded or getattr(fed_response, "semantic_degraded", False):
        response_dict["semantic_degraded"] = True
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

    # #4557 (gap 1): batch had no read gate at all -- a write-only token
    # could read via /query/batch even though /query fails it closed.
    # Batch takes no ``federated`` param (single-zone-only), so both of
    # /query's single-zone checks apply unconditionally here.
    from nexus.bricks.search.federated_search import token_zone_filter_from_auth

    token_zone_filter = token_zone_filter_from_auth(auth_result, root_zone_id=ROOT_ZONE_ID)
    if token_zone_filter is not None:
        if not token_zone_filter:
            raise HTTPException(403, "Token has no zone with read permission for search")
        if zone_id not in token_zone_filter:
            raise HTTPException(403, f"Token has no read permission for zone {zone_id}")

    if not search_daemon.is_initialized:
        raise HTTPException(status_code=503, detail="Search daemon is still initializing")

    # Same ReBAC hook the single-query endpoint uses.
    permission_enforcer = getattr(request.app.state, "permission_enforcer", None)
    op_context = get_operation_context(auth_result)
    overfetch_multiplier = 3 if permission_enforcer is not None else 1

    # Over-fetch per-query so ReBAC filtering does not strip us below the
    # caller's requested limit. Keep caller's original limit for trimming.
    requested_limits: list[int] = []
    fetch_queries: list[dict[str, Any]] = []
    for q_spec in raw_queries:
        orig_limit = max(1, int(q_spec.get("limit", 10)))
        query_text = str(q_spec.get("query") or q_spec.get("q") or "")
        path_filter = q_spec.get("path_filter", q_spec.get("path"))
        requested_limits.append(orig_limit)
        fetch_queries.append(
            {
                **q_spec,
                "query": query_text,
                "path_filter": path_filter,
                "limit": orig_limit * overfetch_multiplier,
            }
        )

    t0 = time.perf_counter()
    raw_results = await search_daemon.batch_search(fetch_queries, zone_id=zone_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    filter_ms_total = 0.0
    response_queries: list[dict[str, Any]] = []
    for q_spec, results, orig_limit in zip(raw_queries, raw_results, requested_limits, strict=True):
        # File-level ReBAC filtering (Decision #17) — same enforcement as /query.
        filtered, filter_ms = _apply_rebac_filter(
            results,
            permission_enforcer,
            auth_result,
            zone_id,
            operation_context=op_context,
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
            title = getattr(r, "title_score", None)
            if title is not None:
                entry["title_score"] = round(title, 4)
            ctx = getattr(r, "context", None)
            if ctx is not None:
                entry["context"] = ctx
            # Issue #4544 (Codex review R1): batch hits must carry the same
            # omit-when-None tier_boost attribution as single-query results,
            # or batch consumers see multiplied scores they cannot explain.
            tier_boost = getattr(r, "tier_boost", None)
            if tier_boost is not None:
                entry["tier_boost"] = round(tier_boost, 4)
            formatted.append(entry)
        response_queries.append(
            {
                "query": q_spec.get("query") or q_spec.get("q", ""),
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
    section: str | None = None,
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

    target_zone = target_zone_for_context(op_context, {"path": path, "files": files})

    async def _work() -> dict[str, Any]:
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
            if section is not None:
                grep_kwargs["section"] = section
            raw_results = await search_service.grep(**grep_kwargs)
        except (ValueError, InvalidPathError) as exc:
            # Client errors from SearchService:
            #  * ValueError — invalid regex, size cap exceeded, cross-zone entry
            #  * InvalidPathError — path traversal segment in ``path`` or ``files``
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("grep failed: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"grep failed: {type(exc).__name__}"
            ) from exc

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
            operation_context=op_context,
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
        if section is not None:
            from nexus.server.rpc.handlers.filesystem import _section_response_meta

            extras.update(_section_response_meta(section, paginated))
        return build_paginated_list_response(
            items=paginated,
            total=total,
            offset=offset,
            limit=limit,
            extras=extras,
            has_more=has_more,
        )

    return await run_zone_scoped(_get_zone_registry(request), target_zone, _work)


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

    target_zone = target_zone_for_context(op_context, {"path": path, "files": files})

    async def _work() -> dict[str, Any]:
        try:
            all_matches: list[str] = search_service.glob(
                pattern=pattern, path=path, context=op_context, files=files
            )
        except (ValueError, InvalidPathError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("glob failed: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"glob failed: {type(exc).__name__}"
            ) from exc

        # #3731: path_extractor=identity eliminates the _GlobResultShim shim class.
        pre_filter_count = len(all_matches)
        filtered_paths, filter_ms = _apply_rebac_filter(
            all_matches,
            permission_enforcer,
            auth_result,
            zone_id,
            path_extractor=lambda p: p,
            operation_context=op_context,
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

    return await run_zone_scoped(_get_zone_registry(request), target_zone, _work)


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


def _resolve_section_filter(section: str | None, in_section: str | None) -> str | None:
    """Resolve the canonical section filter from supported aliases."""
    if section is not None and not isinstance(section, str):
        raise HTTPException(status_code=400, detail="Field 'section' must be a string")
    if in_section is not None and not isinstance(in_section, str):
        raise HTTPException(status_code=400, detail="Field 'in_section' must be a string")
    values = [value for value in (section, in_section) if value is not None]
    if not values:
        return None
    if len(values) == 2 and values[0] != values[1]:
        raise HTTPException(
            status_code=400,
            detail="Fields 'section' and 'in_section' must match when both are provided",
        )
    resolved = values[0]
    if not resolved.strip():
        raise HTTPException(status_code=400, detail="section must be a non-empty string")
    return resolved


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
    section: str | None = Query(
        None,
        description=(
            "Restrict matches to a markdown/parsed-content section heading "
            "(#4186), e.g. 'API' or '## API'."
        ),
    ),
    in_section: str | None = Query(
        None,
        description="Alias for section, matching the CLI --in-section flag (#4186).",
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
        section=_resolve_section_filter(section, in_section),
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
    section = _resolve_section_filter(body.get("section"), body.get("in_section"))

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
        section=section,
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

    Issue #4566: documents whose ``file_paths`` projection row hasn't landed
    yet (write-then-index races the operation-log consumer) get a bounded
    server-side wait; anything still unresolved fails closed with HTTP 409
    and ``detail.skipped`` listing the affected paths. Indexing is
    idempotent, so retrying the whole batch after a 409 is safe — documents
    indexed before the 409 stay indexed.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    body = await request.json()
    documents: list[dict[str, Any]] = body.get("documents", [])
    if not documents:
        raise HTTPException(status_code=400, detail="No documents provided")

    async def _work() -> dict[str, Any]:
        try:
            result = await search_daemon.index_documents(documents, zone_id=zone_id)
        except Exception as exc:
            logger.error("index_documents failed: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Index persistence failed: {type(exc).__name__}: {exc}",
            ) from exc
        # ``getattr`` fallbacks keep int-returning test doubles working.
        count = getattr(result, "indexed", result)
        skipped = list(getattr(result, "skipped", []) or [])
        if skipped:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": (
                        "documents skipped: no live file_paths row after the bounded "
                        "projection wait — retry once the write is visible"
                    ),
                    "count": count,
                    "skipped": skipped,
                    "zone_id": zone_id,
                },
            )
        return {"status": "indexed", "count": count, "zone_id": zone_id}

    return await run_zone_scoped(_get_zone_registry(request), _auth_target_zone(auth_result), _work)


@router.post("/refresh")
async def search_refresh_notify(
    request: Request,
    path: str = Query(..., description="Path of the changed file"),
    change_type: str = Query("update", description="Type of change: create, update, delete"),
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Notify the search daemon of a file change for index refresh."""
    from nexus.server.dependencies import get_operation_context

    op_context = get_operation_context(auth_result)
    target_zone = target_zone_for_context(op_context, {"path": path})

    async def _work() -> dict[str, Any]:
        await search_daemon.notify_file_change(path, change_type)
        return {"status": "accepted", "path": path, "change_type": change_type}

    return await run_zone_scoped(_get_zone_registry(request), target_zone, _work)


@router.post("/expand")
async def search_expand(
    request: Request,
    q: str = Query(..., description="Query to expand", min_length=1),
    context: str | None = Query(None, description="Optional context about the collection"),
    model: str = Query("deepseek/deepseek-chat", description="LLM model to use"),
    max_lex: int = Query(2, description="Max lexical variants", ge=0, le=5),
    max_vec: int = Query(2, description="Max vector variants", ge=0, le=5),
    max_hyde: int = Query(2, description="Max HyDE passages", ge=0, le=5),
    auth_result: dict[str, Any] = Depends(require_auth),
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

    async def _work() -> dict[str, Any]:
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

    return await run_zone_scoped(_get_zone_registry(request), _auth_target_zone(auth_result), _work)


# =============================================================================
# Sub-router registrations (#4553 follow-up — 2000-line gate split)
# =============================================================================
#
# ``_search_indexed_dirs`` and ``_search_locate`` are underscore-prefixed
# concern-siblings that import shared helpers (``_get_search_daemon`` &c.)
# from THIS module at their own module-load time.  These imports MUST stay
# at the end of ``search.py`` so the parent's namespace is fully populated
# before the sub-modules resolve their dependencies — moving them higher
# would circular-fault the first import that reached ``search`` via a
# sibling module.
from nexus.server.api.v2.routers._search_indexed_dirs import (  # noqa: E402
    router as _indexed_dirs_router,
)
from nexus.server.api.v2.routers._search_locate import router as _locate_router  # noqa: E402

router.include_router(_indexed_dirs_router)
router.include_router(_locate_router)
