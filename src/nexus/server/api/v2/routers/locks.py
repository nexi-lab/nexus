"""Lock API v2 router (#2056).

Provides distributed locking endpoints using Redis/Dragonfly:
- POST   /api/v2/locks              — acquire a lock
- GET    /api/v2/locks              — list active locks
- GET    /api/v2/locks/{path:path}  — get lock status
- DELETE /api/v2/locks/{path:path}  — release a lock
- PATCH  /api/v2/locks/{path:path}  — extend a lock TTL

Ported from v1 with improvements:
- Pydantic request/response models (already existed in v1)
- Extracted helpers (_to_lock_response, _normalize_path) for DRY
- fence_token returned directly from acquire (no extra get_lock_info call)
"""

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.api.v2.models.locks import (
    LockAcquireRequest,
    LockExtendRequest,
    LockHolderResponse,
    LockInfoMutex,
    LockInfoSemaphore,
    LockListResponse,
    LockResponse,
    LockStatusResponse,
)
from nexus.server.dependencies import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/locks", tags=["locks"])

# =============================================================================
# Dependencies
# =============================================================================


def _get_lock_manager(request: Request) -> Any:
    """Get the distributed lock manager from NexusFS, raising 503 if not configured."""
    fs = getattr(request.app.state, "nexus_fs", None)
    if fs is None:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    lock_mgr = getattr(fs, "_lock_manager", None)
    if lock_mgr is None:
        raise HTTPException(
            status_code=503,
            detail="Distributed lock manager not configured. "
            "Enable Redis/Dragonfly for distributed locking.",
        )
    return lock_mgr


# =============================================================================
# Helpers
# =============================================================================


def _normalize_path(path: str) -> str:
    """Ensure path has a leading slash."""
    return path if path.startswith("/") else "/" + path


def _to_lock_info_response(lock_info: Any) -> LockInfoMutex | LockInfoSemaphore:
    """Convert a LockInfo dataclass to the appropriate response model."""
    if lock_info.mode == "mutex" and lock_info.holders:
        h = lock_info.holders[0]
        return LockInfoMutex(
            lock_id=h.lock_id,
            holder_info=h.holder_info,
            acquired_at=h.acquired_at,
            expires_at=h.expires_at,
            fence_token=lock_info.fence_token,
        )
    return LockInfoSemaphore(
        max_holders=lock_info.max_holders,
        holders=[
            LockHolderResponse(
                lock_id=h.lock_id,
                holder_info=h.holder_info,
                acquired_at=h.acquired_at,
                expires_at=h.expires_at,
            )
            for h in lock_info.holders
        ],
        current_holders=len(lock_info.holders),
        fence_token=lock_info.fence_token,
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", status_code=201, response_model=LockResponse)
async def acquire_lock(
    request: LockAcquireRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(_get_lock_manager),
) -> LockResponse:
    """Acquire a distributed lock on a path.

    Supports both mutex (max_holders=1) and semaphore (max_holders>1) modes.
    Use blocking=false for non-blocking acquisition (returns immediately).
    """
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    path = _normalize_path(request.path)
    timeout = request.timeout if request.blocking else 0.0

    try:
        lock_id = await lock_manager.acquire(
            zone_id=zone_id,
            path=path,
            timeout=timeout,
            ttl=request.ttl,
            max_holders=request.max_holders,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    if lock_id is None:
        if request.blocking:
            raise HTTPException(
                status_code=409,
                detail=f"Lock acquisition timeout after {request.timeout}s",
            )
        raise HTTPException(
            status_code=409,
            detail="Lock not available (non-blocking mode)",
        )

    expires_at = datetime.now(UTC).timestamp() + request.ttl
    expires_at_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()

    # Get fence token from lock info
    lock_info = await lock_manager.get_lock_info(zone_id, path)
    fence_token = lock_info.fence_token if lock_info else 0

    return LockResponse(
        lock_id=lock_id,
        path=path,
        mode="mutex" if request.max_holders == 1 else "semaphore",
        max_holders=request.max_holders,
        ttl=int(request.ttl),
        expires_at=expires_at_iso,
        fence_token=fence_token,
    )


@router.get("", response_model=LockListResponse)
async def list_locks(
    limit: int = Query(100, ge=1, le=1000, description="Max number of locks to return"),
    pattern: str = Query("*", description="Path pattern filter (glob-style)"),
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(_get_lock_manager),
) -> LockListResponse:
    """List active locks for the current zone."""
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    lock_infos = await lock_manager.list_locks(zone_id, pattern=pattern, limit=limit)

    locks: list[LockInfoMutex | LockInfoSemaphore] = [
        _to_lock_info_response(li) for li in lock_infos
    ]
    return LockListResponse(locks=locks, count=len(locks))


@router.get("/{path:path}", response_model=LockStatusResponse)
async def get_lock_status(
    path: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(_get_lock_manager),
) -> LockStatusResponse:
    """Get lock status for a specific path."""
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    path = _normalize_path(path)

    lock_info = await lock_manager.get_lock_info(zone_id, path)
    if not lock_info:
        return LockStatusResponse(path=path, locked=False, lock_info=None)

    return LockStatusResponse(path=path, locked=True, lock_info=_to_lock_info_response(lock_info))


@router.delete("/{path:path}")
async def release_lock(
    path: str,
    lock_id: str = Query(..., description="Lock ID from acquire response"),
    force: bool = Query(False, description="Force release (admin only)"),
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(_get_lock_manager),
) -> JSONResponse:
    """Release a distributed lock.

    The lock_id must match the ID returned during acquisition.
    Use force=true for admin recovery of stuck locks (requires admin role).
    """
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    path = _normalize_path(path)

    if force:
        if not auth_result.get("is_admin", False):
            raise HTTPException(status_code=403, detail="Force release requires admin privileges")
        released = await lock_manager.force_release(zone_id, path)
        if not released:
            raise HTTPException(status_code=404, detail=f"No lock found for path: {path}")
        logger.warning("Lock force-released by admin: zone=%s, path=%s", zone_id, path)
        return JSONResponse(content={"released": True, "forced": True})

    released = await lock_manager.release(lock_id, zone_id, path)
    if not released:
        raise HTTPException(
            status_code=403,
            detail="Lock release failed: not owned by this lock_id or already expired",
        )
    return JSONResponse(content={"released": True})


@router.patch("/{path:path}", response_model=LockResponse)
async def extend_lock(
    path: str,
    request: LockExtendRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(_get_lock_manager),
) -> LockResponse:
    """Extend a lock's TTL (heartbeat).

    Call this periodically (e.g., every TTL/2) to keep long-running
    operations alive. The lock must be owned by the caller (lock_id match).
    """
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    path = _normalize_path(path)

    extended = await lock_manager.extend(request.lock_id, zone_id, path, ttl=request.ttl)
    if not extended.success:
        raise HTTPException(
            status_code=403,
            detail="Lock extend failed: not owned by this lock_id or already expired",
        )

    expires_at = datetime.now(UTC).timestamp() + request.ttl
    expires_at_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()

    lock_info = extended.lock_info
    mode: Literal["mutex", "semaphore"] = "mutex"
    max_holders = 1
    fence_token = 0

    if lock_info:
        mode = lock_info.mode
        fence_token = lock_info.fence_token
        max_holders = lock_info.max_holders

    return LockResponse(
        lock_id=request.lock_id,
        path=path,
        mode=mode,
        max_holders=max_holders,
        ttl=int(request.ttl),
        expires_at=expires_at_iso,
        fence_token=fence_token,
    )
