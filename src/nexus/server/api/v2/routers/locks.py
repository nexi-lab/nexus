"""Distributed locks REST API router.

Exposes the kernel's advisory lock syscalls (sys_lock / sys_unlock /
sys_readdir on /__sys__/locks/) via REST endpoints for the TUI.

Issue #3250: TUI Locks tab.
"""

import logging
import time
import uuid
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from nexus.server.dependencies import get_operation_context, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/locks", tags=["locks"])

# In-memory lock store (fallback when kernel not available)
_locks: dict[str, dict[str, Any]] = {}


class AcquireRequest(BaseModel):
    mode: str = "mutex"
    ttl_seconds: int = 60


class ExtendRequest(BaseModel):
    lock_id: str
    ttl: int = 60


def _get_nexus_fs(request: Request) -> Any:
    """Get the NexusFS instance from app state."""
    return getattr(request.app.state, "nexus_fs", None)


def _normalize_mode(mode: str) -> str:
    """Map API lock modes to kernel advisory lock modes."""
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
async def list_locks(
    request: Request,
    auth_result: dict = Depends(require_auth),
) -> dict[str, Any]:
    """List all active locks (admin/system only).

    Issue #3786 / Codex Round 8 finding #3: previously this handler
    invoked sys_readdir without an OperationContext, which the readdir
    gate treated as privileged.  Now we resolve the caller and require
    admin/system, then propagate that context downstream so the gate
    matches the policy stated in the readdir branch.
    """
    ctx = get_operation_context(auth_result)
    if not (getattr(ctx, "is_admin", False) or getattr(ctx, "is_system", False)):
        raise HTTPException(status_code=403, detail="locks listing requires admin")
    nx = _get_nexus_fs(request)
    if nx and hasattr(nx, "sys_readdir"):
        try:
            kernel_locks = nx.sys_readdir("/__sys__/locks/", details=True, context=ctx)
            locks = [
                {
                    "lock_id": holder.get("lock_id", ""),
                    "resource": lock_info.get("path", ""),
                    "mode": holder.get("mode", "exclusive"),
                    "max_holders": lock_info.get("max_holders", 1),
                    "holder_info": holder.get("holder_info", ""),
                    "acquired_at": holder.get("acquired_at_secs", 0),
                    "expires_at": holder.get("expires_at_secs", 0),
                }
                for lock_info in kernel_locks
                for holder in lock_info.get("holders", [])
            ]
            return {"locks": locks, "count": len(locks)}
        except Exception as e:
            logger.debug("Kernel lock list failed: %s", e)

    # Fallback: in-memory store
    now = time.time()
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
    nx = _get_nexus_fs(request)
    if nx and hasattr(nx, "sys_lock"):
        try:
            mode = "exclusive" if body.mode == "mutex" else body.mode
            lock_id = nx.sys_lock(resource, mode=mode, ttl=body.ttl_seconds)
            if lock_id:
                return {"status": "acquired", "lock_id": lock_id, "resource": resource}
            raise HTTPException(status_code=409, detail=f"Resource already locked: {resource}")
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
    nx = _get_nexus_fs(request)
    if nx and hasattr(nx, "sys_unlock"):
        try:
            nx.sys_unlock(resource, lock_id=lock_id)
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
    nx = _get_nexus_fs(request)
    if nx and hasattr(nx, "sys_lock"):
        try:
            # sys_lock with existing lock_id = extend TTL
            result = nx.sys_lock(resource, lock_id=body.lock_id, ttl=body.ttl)
            if result:
                return {"status": "extended", "resource": resource}
        except Exception as e:
            logger.debug("Kernel lock extend failed: %s", e)

    if resource not in _locks:
        raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")
    _locks[resource]["expires_at"] = time.time() + body.ttl
    return {"status": "extended", **_locks[resource]}
