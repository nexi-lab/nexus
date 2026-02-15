"""Sync push REST API endpoint (Issue #1129).

Triggers an immediate write-back push for a given mount point,
flushing pending backlog entries to the source backend.

    POST /api/v2/sync/mounts/{mount_point}/push
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException

from nexus.server.api.v2.dependencies import (
    _get_require_auth,
    get_write_back_service,
)
from nexus.server.api.v2.models.sync_push import SyncPushResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/sync", tags=["sync"])


async def _require_admin(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict[str, Any]:
    """Require admin role for sync push."""
    if not auth_result.get("is_admin", False):
        raise HTTPException(status_code=403, detail="Admin role required for sync push")
    return auth_result


@router.post("/mounts/{mount_point:path}/push", response_model=SyncPushResponse)
async def push_mount(
    mount_point: str,
    _auth_result: dict[str, Any] = Depends(_require_admin),
    service: Any = Depends(get_write_back_service),
) -> SyncPushResponse:
    """Trigger an immediate write-back push for a mount point.

    Processes all pending backlog entries for the given mount,
    pushing changes to the source backend. Returns push statistics.
    """
    # URL-decode the mount point (e.g. %2Fmnt%2Flocal -> /mnt/local)
    decoded_mount = unquote(mount_point)
    if not decoded_mount.startswith("/"):
        decoded_mount = "/" + decoded_mount

    # Resolve backend_name and zone_id from mount info
    mount_info = service.get_mount_for_path(decoded_mount)
    if mount_info is None:
        raise HTTPException(
            status_code=404,
            detail=f"No mount found at {decoded_mount}",
        )

    backend_name = mount_info["backend_name"]
    zone_id = mount_info.get("zone_id", "default")

    # Snapshot metrics before push
    before = service.get_metrics_snapshot()

    try:
        await service.push_mount(backend_name, zone_id)
    except Exception:
        logger.error("[WRITE_BACK] Push failed for %s", decoded_mount, exc_info=True)
        raise HTTPException(status_code=500, detail="Push operation failed") from None

    # Snapshot metrics after push and compute deltas
    after = service.get_metrics_snapshot()
    pushed = after["changes_pushed"] - before["changes_pushed"]
    failed = after["changes_failed"] - before["changes_failed"]
    conflicts = after["conflicts_detected"] - before["conflicts_detected"]

    return SyncPushResponse(
        mount_point=decoded_mount,
        changes_pushed=pushed,
        changes_failed=failed,
        conflicts_detected=conflicts,
        metrics=after,
    )
