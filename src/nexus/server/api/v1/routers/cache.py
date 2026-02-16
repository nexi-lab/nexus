"""Cache API router (Issue #1288).

Provides cache management endpoints:
- POST /api/cache/warmup    -- pre-populate caches for faster access
- GET  /api/cache/stats     -- get cache statistics for all cache layers
- GET  /api/cache/hot-files -- get frequently accessed files

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.cache.warmer import CacheWarmer, WarmupConfig, get_file_access_tracker
from nexus.server.api.v1.dependencies import get_nexus_fs
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cache"])


@router.post("/api/cache/warmup")
async def warmup_cache(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> dict[str, Any]:
    """Pre-populate caches for faster access.

    Accepts a JSON body with:
    - path: directory path to warm up (mutually exclusive with user)
    - user: warm up based on user access history (mutually exclusive with path)
    - hours: lookback window for user history (default: 24)
    - depth: directory traversal depth (default: 2)
    - include_content: whether to cache file content (default: false)
    - max_files: maximum files to warm (default: 1000)
    """
    body = await request.json()

    path = body.get("path")
    user = body.get("user")
    hours = body.get("hours", 24)
    depth = body.get("depth", 2)
    include_content = body.get("include_content", False)
    max_files = body.get("max_files", 1000)
    zone_id = auth_result.get("zone_id", "default")

    config = WarmupConfig(
        max_files=max_files,
        depth=depth,
        include_content=include_content,
    )
    file_tracker = get_file_access_tracker() if user else None

    warmer = CacheWarmer(
        nexus_fs=nexus_fs,
        config=config,
        file_tracker=file_tracker,
    )

    if user:
        stats = await warmer.warmup_from_history(
            user=user,
            hours=hours,
            max_files=max_files,
            zone_id=zone_id,
        )
    elif path:
        stats = await warmer.warmup_directory(
            path=path,
            depth=depth,
            include_content=include_content,
            max_files=max_files,
            zone_id=zone_id,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either 'path' or 'user' must be provided",
        )

    return {"status": "completed", **stats.to_dict()}


@router.get("/api/cache/stats")
async def get_cache_stats(
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> dict[str, Any]:
    """Get cache statistics for all cache layers.

    Returns stats for metadata_cache, content_cache, permission_cache,
    tiger_cache, dir_visibility_cache, and file_access_tracker.
    """
    cache_stats: dict[str, Any] = {}

    # Metadata cache stats
    cache = getattr(nexus_fs, "metadata_cache", None)
    if cache is None and hasattr(nexus_fs, "metadata"):
        cache = getattr(nexus_fs.metadata, "_cache", None)
    if cache:
        cache_stats["metadata_cache"] = {
            "path_cache_size": len(getattr(cache, "_path_cache", {})),
            "list_cache_size": len(getattr(cache, "_list_cache", {})),
            "exists_cache_size": len(getattr(cache, "_exists_cache", {})),
        }

    # Content cache stats
    if hasattr(nexus_fs, "backend") and hasattr(nexus_fs.backend, "content_cache"):
        cc = nexus_fs.backend.content_cache
        if cc and hasattr(cc, "get_stats"):
            cache_stats["content_cache"] = cc.get_stats()

    # Permission cache stats
    if hasattr(nexus_fs, "_rebac_manager"):
        rm = nexus_fs._rebac_manager
        if hasattr(rm, "_permission_cache") and rm._permission_cache:
            pc = rm._permission_cache
            if hasattr(pc, "get_stats"):
                cache_stats["permission_cache"] = pc.get_stats()
        if hasattr(rm, "_tiger_cache") and rm._tiger_cache:
            tc = rm._tiger_cache
            if hasattr(tc, "get_stats"):
                cache_stats["tiger_cache"] = tc.get_stats()

    # Directory visibility cache
    if hasattr(nexus_fs, "_dir_visibility_cache") and nexus_fs._dir_visibility_cache:
        dvc = nexus_fs._dir_visibility_cache
        if hasattr(dvc, "get_metrics"):
            cache_stats["dir_visibility_cache"] = dvc.get_metrics()

    # Issue #1169: Read-set-aware cache stats (precision metrics)
    read_set_cache = getattr(nexus_fs, "read_set_cache", None)
    if read_set_cache is not None:
        cache_stats["read_set_cache"] = read_set_cache.get_stats()

    # Issue #1169: ReadSetRegistry stats
    read_set_registry = getattr(nexus_fs, "read_set_registry", None)
    if read_set_registry is not None:
        cache_stats["read_set_registry"] = read_set_registry.get_stats()

    # File access tracker stats
    tracker = get_file_access_tracker()
    cache_stats["file_access_tracker"] = tracker.get_stats()

    return cache_stats


@router.get("/api/cache/hot-files")
async def get_hot_files(
    limit: int = Query(20, ge=1, le=100),
    auth_result: dict[str, Any] = Depends(require_auth),
) -> list[dict[str, Any]]:
    """Get frequently accessed files.

    Returns a list of hot files sorted by access frequency, including
    path, zone_id, access_count, last_access, and total_bytes.

    Args:
        limit: Maximum number of hot files to return (1-100, default: 20).
    """
    zone_id = auth_result.get("zone_id", "default")
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
