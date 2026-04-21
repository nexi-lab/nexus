"""Distributed locks REST API router.

Exposes the kernel's LockManager via REST endpoints for the TUI.
Uses in-memory lock state (mirrors what the kernel LockManager tracks).

Issue #3250: TUI Locks tab.
"""

import logging
import time
import uuid
from dataclasses import asdict, is_dataclass
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


def _normalize_mode(mode: str) -> str:
    """Map API lock modes to AdvisoryLockManager modes."""
    mode_norm = (mode or "mutex").strip().lower()
    if mode_norm in {"shared", "read", "reader"}:
        return "shared"
    return "exclusive"


def _holder_to_dict(holder: Any) -> dict[str, Any]:
    """Normalize holder info from dict/dataclass/object to dict."""
    if holder is None:
        return {}
    if isinstance(holder, dict):
        return holder
    if is_dataclass(holder) and not isinstance(holder, type):
        return asdict(holder)
    return {
        "lock_id": getattr(holder, "lock_id", None),
        "holder_info": getattr(holder, "holder_info", ""),
        "acquired_at": getattr(holder, "acquired_at", 0),
        "expires_at": getattr(holder, "expires_at", 0),
    }


def _lock_to_response(lock: Any) -> dict[str, Any]:
    """Normalize lock info from manager/list call into API response shape."""
    if lock is None:
        return {}
    if isinstance(lock, dict):
        holders = lock.get("holders", [])
        first_holder = _holder_to_dict(holders[0]) if holders else {}
        return {
            "lock_id": lock.get("lock_id") or first_holder.get("lock_id") or str(uuid.uuid4()),
            "resource": lock.get("resource", lock.get("path", "")),
            "mode": lock.get("mode", "mutex"),
            "max_holders": lock.get("max_holders", 1),
            "holder_info": lock.get("holder_info", first_holder.get("holder_info", "")),
            "acquired_at": lock.get("acquired_at", first_holder.get("acquired_at", 0)),
            "expires_at": lock.get("expires_at", first_holder.get("expires_at", 0)),
            "fence_token": lock.get("fence_token", 0),
        }

    # Dataclass/object shape (e.g. LockInfo + HolderInfo).
    holders = list(getattr(lock, "holders", []) or [])
    first_holder = _holder_to_dict(holders[0]) if holders else {}
    return {
        "lock_id": first_holder.get("lock_id") or str(uuid.uuid4()),
        "resource": getattr(lock, "resource", getattr(lock, "path", "")),
        "mode": getattr(lock, "mode", "mutex"),
        "max_holders": getattr(lock, "max_holders", 1),
        "holder_info": first_holder.get("holder_info", ""),
        "acquired_at": first_holder.get("acquired_at", 0),
        "expires_at": first_holder.get("expires_at", 0),
        "fence_token": getattr(lock, "fence_token", 0),
    }


def _mgr_release(mgr: Any, lock_id: str, resource: str) -> bool:
    """Call manager.release with cross-version signature compatibility."""
    try:
        return bool(mgr.release(lock_id, resource))
    except TypeError:
        try:
            return bool(mgr.release(resource, lock_id=lock_id))
        except TypeError:
            return bool(mgr.release(resource, lock_id))


def _mgr_extend(mgr: Any, lock_id: str, resource: str, ttl: int) -> Any:
    """Call manager.extend with cross-version signature compatibility."""
    try:
        return mgr.extend(lock_id, resource, ttl=float(ttl))
    except TypeError:
        try:
            return mgr.extend(resource, lock_id=lock_id, ttl=ttl)
        except TypeError:
            return mgr.extend(resource, lock_id, ttl)


@router.get("")
async def list_locks(request: Request) -> dict[str, Any]:
    """List all active locks."""
    # Try kernel LockManager first
    mgr = _get_lock_manager(request)
    if mgr and hasattr(mgr, "list_locks"):
        try:
            try:
                kernel_locks = mgr.list_locks(pattern="", limit=1000)
            except TypeError:
                kernel_locks = mgr.list_locks()
            locks = [_lock_to_response(lock) for lock in (kernel_locks or [])]
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
            mode = _normalize_mode(body.mode)
            lock_id = mgr.acquire(resource, mode=mode, ttl=float(body.ttl_seconds))
            if lock_id is None:
                raise HTTPException(status_code=409, detail=f"Resource already locked: {resource}")

            # Keep mirror state for compatibility/fallback paths.
            now = time.time()
            _locks[resource] = {
                "lock_id": str(lock_id),
                "resource": resource,
                "mode": "mutex" if mode == "exclusive" else "shared",
                "max_holders": 1,
                "holder_info": "kernel",
                "acquired_at": now,
                "expires_at": now + body.ttl_seconds,
                "fence_token": 0,
            }
            return {"status": "acquired", "lock_id": str(lock_id), "resource": resource}
        except HTTPException:
            raise
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
            released = _mgr_release(mgr, lock_id, resource)
            if not released:
                raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")
            _locks.pop(resource, None)
            return
        except HTTPException:
            raise
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
            result = _mgr_extend(mgr, body.lock_id, resource, body.ttl)
            success = bool(getattr(result, "success", result))
            if not success:
                raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")

            if resource in _locks:
                _locks[resource]["expires_at"] = time.time() + body.ttl
            return {"status": "extended", "resource": resource}
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("Kernel lock extend failed: %s", e)

    if resource not in _locks:
        raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")
    _locks[resource]["expires_at"] = time.time() + body.ttl
    return {"status": "extended", **_locks[resource]}
