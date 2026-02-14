"""Lock API router (Issue #1186, #1288).

Provides distributed locking endpoints using Redis/Dragonfly:
- POST   /api/locks              — acquire a lock
- GET    /api/locks              — list active locks
- GET    /api/locks/{path:path}  — get lock status
- DELETE /api/locks/{path:path}  — release a lock
- PATCH  /api/locks/{path:path}  — extend a lock TTL

Extracted from ``fastapi_server.py`` during monolith decomposition (#1288).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from nexus.server.api.v1.dependencies import get_lock_manager
from nexus.server.api.v1.models.locks import (
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

router = APIRouter(tags=["locks"])


@router.post("/api/locks", status_code=201, response_model=LockResponse)
async def acquire_lock(
    request: LockAcquireRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(get_lock_manager),
) -> LockResponse:
    """Acquire a distributed lock on a path.

    Supports both mutex (max_holders=1) and semaphore (max_holders>1) modes.
    Use blocking=false for non-blocking acquisition (returns immediately).
    """
    zone_id = auth_result.get("zone_id") or "default"

    # Normalize path to ensure leading slash
    path = request.path if request.path.startswith("/") else "/" + request.path

    # Non-blocking mode: use timeout=0
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
        # SSOT violation (max_holders mismatch)
        raise HTTPException(status_code=409, detail=str(e)) from e

    if lock_id is None:
        if request.blocking:
            raise HTTPException(
                status_code=409,
                detail=f"Lock acquisition timeout after {request.timeout}s",
            )
        else:
            raise HTTPException(
                status_code=409,
                detail="Lock not available (non-blocking mode)",
            )

    # Calculate expiration time
    expires_at = datetime.now(UTC).timestamp() + request.ttl
    expires_at_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()

    # Get lock info to retrieve fence token
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


@router.get("/api/locks", response_model=LockListResponse)
async def list_locks(
    limit: int = Query(100, ge=1, le=1000, description="Max number of locks to return"),
    pattern: str = Query("*", description="Path pattern filter (glob-style)"),
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(get_lock_manager),
) -> LockListResponse:
    """List active locks for the current zone."""
    zone_id = auth_result.get("zone_id") or "default"

    lock_infos = await lock_manager.list_locks(zone_id, pattern=pattern, limit=limit)

    # Convert LockInfo dataclasses to response models
    locks: list[LockInfoMutex | LockInfoSemaphore] = []
    for lock_info in lock_infos:
        if lock_info.mode == "mutex" and lock_info.holders:
            h = lock_info.holders[0]
            locks.append(
                LockInfoMutex(
                    lock_id=h.lock_id,
                    holder_info=h.holder_info,
                    acquired_at=h.acquired_at,
                    expires_at=h.expires_at,
                    fence_token=lock_info.fence_token,
                )
            )
        else:
            locks.append(
                LockInfoSemaphore(
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
            )

    return LockListResponse(locks=locks, count=len(locks))


@router.get("/api/locks/{path:path}", response_model=LockStatusResponse)
async def get_lock_status(
    path: str,
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(get_lock_manager),
) -> LockStatusResponse:
    """Get lock status for a specific path."""
    zone_id = auth_result.get("zone_id") or "default"

    # Normalize path to ensure leading slash (URL path captures without leading /)
    if not path.startswith("/"):
        path = "/" + path

    lock_info = await lock_manager.get_lock_info(zone_id, path)
    if not lock_info:
        return LockStatusResponse(path=path, locked=False, lock_info=None)

    # Convert LockInfo dataclass to response model
    lock_info_response: LockInfoMutex | LockInfoSemaphore
    if lock_info.mode == "mutex" and lock_info.holders:
        h = lock_info.holders[0]
        lock_info_response = LockInfoMutex(
            lock_id=h.lock_id,
            holder_info=h.holder_info,
            acquired_at=h.acquired_at,
            expires_at=h.expires_at,
            fence_token=lock_info.fence_token,
        )
    else:
        lock_info_response = LockInfoSemaphore(
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

    return LockStatusResponse(path=path, locked=True, lock_info=lock_info_response)


@router.delete("/api/locks/{path:path}")
async def release_lock(
    path: str,
    lock_id: str = Query(..., description="Lock ID from acquire response"),
    force: bool = Query(False, description="Force release (admin only)"),
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(get_lock_manager),
) -> JSONResponse:
    """Release a distributed lock.

    The lock_id must match the ID returned during acquisition.
    Use force=true for admin recovery of stuck locks (requires admin role).
    """
    zone_id = auth_result.get("zone_id") or "default"

    # Normalize path to ensure leading slash (URL path captures without leading /)
    if not path.startswith("/"):
        path = "/" + path

    if force:
        if not auth_result.get("is_admin", False):
            raise HTTPException(status_code=403, detail="Force release requires admin privileges")
        released = await lock_manager.force_release(zone_id, path)
        if not released:
            raise HTTPException(status_code=404, detail=f"No lock found for path: {path}")
        logger.warning("Lock force-released by admin: zone=%s, path=%s", zone_id, path)
        return JSONResponse(content={"released": True, "forced": True})

    # Normal release with ownership check
    released = await lock_manager.release(lock_id, zone_id, path)
    if not released:
        raise HTTPException(
            status_code=403,
            detail="Lock release failed: not owned by this lock_id or already expired",
        )
    return JSONResponse(content={"released": True})


@router.patch("/api/locks/{path:path}", response_model=LockResponse)
async def extend_lock(
    path: str,
    request: LockExtendRequest,
    auth_result: dict[str, Any] = Depends(require_auth),
    lock_manager: Any = Depends(get_lock_manager),
) -> LockResponse:
    """Extend a lock's TTL (heartbeat).

    Call this periodically (e.g., every TTL/2) to keep long-running
    operations alive. The lock must be owned by the caller (lock_id match).
    """
    zone_id = auth_result.get("zone_id") or "default"

    # Normalize path to ensure leading slash (URL path captures without leading /)
    if not path.startswith("/"):
        path = "/" + path

    extended = await lock_manager.extend(request.lock_id, zone_id, path, ttl=request.ttl)
    if not extended.success:
        raise HTTPException(
            status_code=403,
            detail="Lock extend failed: not owned by this lock_id or already expired",
        )

    # Calculate new expiration
    expires_at = datetime.now(UTC).timestamp() + request.ttl
    expires_at_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()

    # Use lock_info from ExtendResult
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
