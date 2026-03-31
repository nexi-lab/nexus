"""Distributed locks REST API router.

Exposes the kernel's LockManager via REST endpoints for the TUI.
Uses in-memory lock state (mirrors what the kernel LockManager tracks).

Issue #3250: TUI Locks tab.
"""

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/locks", tags=["locks"])

# In-memory lock store (mirrors kernel LockManager state)
_locks: dict[str, dict[str, Any]] = {}


class AcquireRequest(BaseModel):
    mode: str = "mutex"
    ttl_seconds: int = 60


class ExtendRequest(BaseModel):
    lock_id: str
    ttl: int = 60


def _get_lock_manager(request: Request) -> Any:
    """Try to get the kernel's LockManager."""
    nx = getattr(request.app.state, "nexus_fs", None)
    if nx and hasattr(nx, "_lock_manager"):
        return nx._lock_manager
    return None


@router.get("")
async def list_locks(request: Request) -> dict[str, Any]:
    """List all active locks."""
    # Try kernel LockManager first
    mgr = _get_lock_manager(request)
    if mgr and hasattr(mgr, "list_locks"):
        try:
            kernel_locks = mgr.list_locks()
            locks = [
                {
                    "lock_id": lock.get("lock_id", str(uuid.uuid4())),
                    "resource": lock.get("resource", lock.get("path", "")),
                    "mode": lock.get("mode", "mutex"),
                    "max_holders": lock.get("max_holders", 1),
                    "holder_info": lock.get("holder_info", lock.get("holder", "")),
                    "acquired_at": lock.get("acquired_at", 0),
                    "expires_at": lock.get("expires_at", 0),
                    "fence_token": lock.get("fence_token", 0),
                }
                for lock in kernel_locks
            ]
            return {"locks": locks, "count": len(locks)}
        except Exception as e:
            logger.debug("Kernel lock list failed: %s", e)

    # Fallback: in-memory store
    now = time.time()
    # Clean expired locks
    expired = [k for k, v in _locks.items() if v["expires_at"] < now]
    for k in expired:
        del _locks[k]

    locks = list(_locks.values())
    return {"locks": locks, "count": len(locks)}


@router.post("/{resource:path}/acquire")
async def acquire_lock(
    resource: str,
    body: AcquireRequest,
    request: Request,
) -> dict[str, Any]:
    """Acquire a lock on a resource path."""
    mgr = _get_lock_manager(request)
    if mgr and hasattr(mgr, "acquire"):
        try:
            result = mgr.acquire(resource, mode=body.mode, ttl=body.ttl_seconds)
            return {"status": "acquired", "lock_id": str(result), "resource": resource}
        except Exception as e:
            logger.debug("Kernel lock acquire failed: %s", e)

    # Fallback: in-memory
    now = time.time()
    if resource in _locks and _locks[resource]["expires_at"] > now:
        raise HTTPException(status_code=409, detail=f"Resource already locked: {resource}")

    lock_id = str(uuid.uuid4())
    lock = {
        "lock_id": lock_id,
        "resource": resource,
        "mode": body.mode,
        "max_holders": 1,
        "holder_info": "admin",
        "acquired_at": now,
        "expires_at": now + body.ttl_seconds,
        "fence_token": int(now * 1000) % 1000000,
    }
    _locks[resource] = lock
    return {"status": "acquired", **lock}


@router.delete("/{resource:path}")
async def release_lock(
    resource: str,
    lock_id: str,
    request: Request,
) -> None:
    """Release a lock."""
    mgr = _get_lock_manager(request)
    if mgr and hasattr(mgr, "release"):
        try:
            mgr.release(resource, lock_id=lock_id)
            _locks.pop(resource, None)
            return
        except Exception as e:
            logger.debug("Kernel lock release failed: %s", e)

    if resource not in _locks:
        raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")
    if _locks[resource]["lock_id"] != lock_id:
        raise HTTPException(status_code=403, detail="Lock ID mismatch")
    del _locks[resource]


@router.patch("/{resource:path}")
async def extend_lock(
    resource: str,
    body: ExtendRequest,
    request: Request,
) -> dict[str, Any]:
    """Extend a lock's TTL."""
    mgr = _get_lock_manager(request)
    if mgr and hasattr(mgr, "extend"):
        try:
            mgr.extend(resource, lock_id=body.lock_id, ttl=body.ttl)
            return {"status": "extended", "resource": resource}
        except Exception as e:
            logger.debug("Kernel lock extend failed: %s", e)

    if resource not in _locks:
        raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")
    _locks[resource]["expires_at"] = time.time() + body.ttl
    return {"status": "extended", **_locks[resource]}
