"""``/api/v2/search/locate`` endpoint (Issue #3725).

Extracted from ``routers/search.py`` as part of the search-router split
that removed the 2000-line file-size exemption.  This module is
underscore-prefixed because ``search.py`` includes its ``router`` at
module-load time via ``include_router``; no other caller should import
this module directly (the sub-router is registered against the same
public ``/api/v2/search`` prefix on the parent ``router``).

Shared helpers (``_get_search_daemon``, ``get_operation_context``,
``_apply_rebac_filter`` &c.) are imported from the parent module at
module top — the ``from . import _search_locate`` call in ``search.py``
runs AFTER those definitions, so no circular-import chain.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from nexus.lib.rebac_filter import apply_rebac_filter as _apply_rebac_filter
from nexus.lib.rebac_filter import compute_rebac_fetch_limit as _compute_rebac_fetch_limit
from nexus.lib.rebac_filter import rebac_denial_stats as _rebac_denial_stats
from nexus.server.api.v2.routers._search_deps import _get_search_daemon
from nexus.server.dependencies import get_operation_context, require_auth

logger = logging.getLogger(__name__)

# Sub-router with NO prefix — parent ``search.router`` already carries
# ``/api/v2/search`` and includes this via ``include_router``.
router = APIRouter(tags=["search"])


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
        operation_context=get_operation_context(auth_result),
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
