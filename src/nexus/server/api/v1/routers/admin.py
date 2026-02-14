"""Admin / hotspot detection API router (Issue #921, #1288).

Provides admin endpoints for permission hotspot monitoring:
- GET /api/admin/hotspot-stats  -- hotspot detection statistics
- GET /api/admin/hot-entries    -- current hot permission entries

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from nexus.server.api.v1.dependencies import get_nexus_fs
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


@router.get("/api/v1/admin/hotspot-stats")
async def get_hotspot_stats(
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> dict[str, Any]:
    """Get hotspot detection statistics (Issue #921)."""
    permission_enforcer = getattr(nexus_fs, "_permission_enforcer", None)
    if not permission_enforcer:
        raise HTTPException(status_code=503, detail="Permission enforcer not available")

    hotspot_detector = getattr(permission_enforcer, "_hotspot_detector", None)
    if not hotspot_detector:
        return {"enabled": False, "message": "Hotspot tracking not enabled"}

    stats: dict[str, Any] = hotspot_detector.get_stats()
    return stats


@router.get("/api/v1/admin/hot-entries")
async def get_hot_entries(
    limit: int = Query(10, description="Maximum number of entries", ge=1, le=100),
    _auth_result: dict[str, Any] = Depends(require_auth),
    nexus_fs: Any = Depends(get_nexus_fs),
) -> list[dict[str, Any]]:
    """Get current hot permission entries (Issue #921)."""
    permission_enforcer = getattr(nexus_fs, "_permission_enforcer", None)
    if not permission_enforcer:
        raise HTTPException(status_code=503, detail="Permission enforcer not available")

    hotspot_detector = getattr(permission_enforcer, "_hotspot_detector", None)
    if not hotspot_detector:
        return []

    entries = hotspot_detector.get_hot_entries(limit=limit)
    return [
        {
            "subject_type": e.subject_type,
            "subject_id": e.subject_id,
            "resource_type": e.resource_type,
            "permission": e.permission,
            "zone_id": e.zone_id,
            "access_count": e.access_count,
            "last_access": e.last_access,
        }
        for e in entries
    ]
