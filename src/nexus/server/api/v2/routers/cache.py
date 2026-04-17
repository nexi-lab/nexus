"""Cache API v2 router (#2056).

Provides cache management endpoints:
- POST /api/v2/cache/warmup    -- pre-populate caches for faster access
- GET  /api/v2/cache/stats     -- get cache statistics for all cache layers
- GET  /api/v2/cache/hot-files -- get frequently accessed files

Ported from v1 with improvements:
- Pydantic request model for warmup endpoint
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/cache", tags=["cache"])

# =============================================================================
# Request Models
# =============================================================================


class CacheWarmupRequest(BaseModel):
    """Request model for cache warmup."""

    path: str | None = Field(None, description="Directory path to warm up")
    user: str | None = Field(None, description="Warm up based on user access history")
    hours: int = Field(24, description="Lookback window for user history")
    depth: int = Field(2, description="Directory traversal depth")
    include_content: bool = Field(False, description="Whether to cache file content")
    max_files: int = Field(1000, description="Maximum files to warm")


# =============================================================================
# Dependencies
# =============================================================================


def _get_nexus_fs(request: Request) -> Any:
    """Get NexusFS instance from app.state."""
    fs = getattr(request.app.state, "nexus_fs", None)
    if fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")
    return fs


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/warmup")
async def warmup_cache(
    body: CacheWarmupRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(_get_nexus_fs),
) -> dict[str, Any]:
    """Pre-populate caches for faster access."""
    from nexus.server.cache_warmer import CacheWarmer, WarmupConfig, get_file_access_tracker

    zone_id = auth_result.get("zone_id", ROOT_ZONE_ID)

    config = WarmupConfig(
        max_files=body.max_files,
        depth=body.depth,
        include_content=body.include_content,
    )
    file_tracker = get_file_access_tracker() if body.user else None

    warmer = CacheWarmer(
        nexus_fs=nexus_fs,
        config=config,
        file_tracker=file_tracker,
    )

    if body.user:
        stats = await warmer.warmup_from_history(
            user=body.user,
            hours=body.hours,
            max_files=body.max_files,
            zone_id=zone_id,
        )
    elif body.path:
        stats = await warmer.warmup_directory(
            path=body.path,
            depth=body.depth,
            include_content=body.include_content,
            max_files=body.max_files,
            zone_id=zone_id,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either 'path' or 'user' must be provided",
        )

    return {"status": "completed", **stats.to_dict()}


@router.get("/stats")
async def get_cache_stats(
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(_get_nexus_fs),
) -> dict[str, Any]:
    """Get cache statistics for all cache layers."""
    from nexus.server.cache_warmer import get_file_access_tracker

    cache_stats: dict[str, Any] = {}

    if hasattr(nexus_fs, "backend") and hasattr(nexus_fs.backend, "content_cache"):
        cc = nexus_fs.backend.content_cache
        if cc and hasattr(cc, "get_stats"):
            cache_stats["content_cache"] = cc.get_stats()

    rm = getattr(nexus_fs, "_rebac_manager", None)
    if rm is not None:
        pc = getattr(rm, "_permission_cache", None)
        if pc is not None and hasattr(pc, "get_stats"):
            cache_stats["permission_cache"] = pc.get_stats()
        tc = getattr(rm, "_tiger_cache", None)
        if tc is not None and hasattr(tc, "get_stats"):
            cache_stats["tiger_cache"] = tc.get_stats()

    dvc = getattr(nexus_fs, "_dir_visibility_cache", None)
    if dvc is not None and hasattr(dvc, "get_metrics"):
        cache_stats["dir_visibility_cache"] = dvc.get_metrics()

    tracker = get_file_access_tracker()
    cache_stats["file_access_tracker"] = tracker.get_stats()

    return cache_stats


@router.get("/hot-files")
async def get_hot_files(
    limit: int = Query(20, ge=1, le=100),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> list[dict[str, Any]]:
    """Get frequently accessed files."""
    from nexus.server.cache_warmer import get_file_access_tracker

    zone_id = auth_result.get("zone_id", ROOT_ZONE_ID)
    tracker = get_file_access_tracker()
    hot_files = tracker.get_hot_files(zone_id=zone_id, limit=limit)

    return [
        {
            "path": f.path,
            "zone_id": f.zone_id,
            "access_count": f.access_count,
            "last_access": f.last_access,
            "total_bytes": f.total_bytes,
        }
        for f in hot_files
    ]
