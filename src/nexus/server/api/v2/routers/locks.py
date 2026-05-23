"""Distributed locks REST API router.

Exposes the kernel's advisory lock syscalls (sys_lock / sys_unlock /
sys_readdir on /__sys__/locks/) via REST endpoints for the TUI.

Issue #3250: TUI Locks tab.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from nexus.contracts.exceptions import PermissionDeniedError
from nexus.server.api.v2.models.locks import LockAcquireRequest, LockExtendRequest
from nexus.server.dependencies import get_operation_context, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/locks", tags=["locks"])

# In-memory lock store (fallback when kernel not available)
_locks: dict[str, dict[str, Any]] = {}


class PathAcquireRequest(BaseModel):
    mode: str = "mutex"
    ttl_seconds: int = 60


def _get_nexus_fs(request: Request) -> Any:
    """Get the NexusFS instance from app state."""
    return getattr(request.app.state, "nexus_fs", None)


def _normalize_resource(resource: str) -> str:
    """Normalize URL/body lock resources to the kernel's leading-slash form."""
    cleaned = (resource or "/").strip()
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    if cleaned.startswith("/zone/"):
        from nexus.core.path_utils import split_zone_from_internal_path

        _zone_id, unscoped = split_zone_from_internal_path(cleaned)
        if _zone_id is not None:
            cleaned = unscoped or "/"
    return cleaned


def _normalize_mode(mode: str) -> str:
    """Map API lock modes to kernel advisory lock modes."""
    mode_norm = (mode or "mutex").strip().lower()
    if mode_norm in {"shared", "read", "reader"}:
        return "shared"
    return "exclusive"


def _public_mode(max_holders: int) -> str:
    return "semaphore" if max_holders > 1 else "mutex"


def _expires_at_iso(expires_at: float) -> str:
    return datetime.fromtimestamp(expires_at, UTC).isoformat()


def _lock_holders(lock: dict[str, Any]) -> list[dict[str, Any]]:
    holders = lock.get("holders")
    if isinstance(holders, list):
        return [h for h in holders if isinstance(h, dict)]
    return [
        {
            "lock_id": lock.get("lock_id"),
            "holder_info": lock.get("holder_info", ""),
            "acquired_at": lock.get("acquired_at", 0),
            "expires_at": lock.get("expires_at", 0),
        }
    ]


def _prune_expired_lock(resource: str) -> dict[str, Any] | None:
    """Drop expired holder mirror state and return the current resource lock."""
    lock = _locks.get(resource)
    if not lock:
        return None
    now = time.time()
    holders = [h for h in _lock_holders(lock) if float(h.get("expires_at", 0)) > now]
    if not holders:
        _locks.pop(resource, None)
        return None
    lock["holders"] = holders
    first = holders[0]
    lock["lock_id"] = first.get("lock_id")
    lock["holder_info"] = first.get("holder_info", "")
    lock["acquired_at"] = first.get("acquired_at", 0)
    lock["expires_at"] = max(float(h.get("expires_at", 0)) for h in holders)
    return lock


def _find_holder(lock: dict[str, Any], lock_id: str) -> dict[str, Any] | None:
    return next((h for h in _lock_holders(lock) if h.get("lock_id") == lock_id), None)


def _build_lock_response(
    lock: dict[str, Any], holder: dict[str, Any] | None = None
) -> dict[str, Any]:
    holder = holder or (_lock_holders(lock)[0] if _lock_holders(lock) else {})
    return {
        "lock_id": holder.get("lock_id") or lock["lock_id"],
        "path": lock["resource"],
        "resource": lock["resource"],
        "mode": lock.get("mode", "mutex"),
        "max_holders": lock.get("max_holders", 1),
        "ttl": lock.get("ttl", 0),
        "expires_at": _expires_at_iso(
            float(holder.get("expires_at", lock.get("expires_at", time.time())))
        ),
        "fence_token": lock.get("fence_token", 0),
    }


def _status_info_from_lock(lock: dict[str, Any]) -> dict[str, Any]:
    holders = _lock_holders(lock)
    if lock.get("max_holders", 1) > 1:
        return {
            "mode": "semaphore",
            "max_holders": lock.get("max_holders", 1),
            "holders": [
                {
                    "lock_id": holder.get("lock_id"),
                    "holder_info": holder.get("holder_info", ""),
                    "acquired_at": holder.get("acquired_at", 0),
                    "expires_at": holder.get("expires_at", 0),
                }
                for holder in holders
            ],
            "current_holders": len(holders),
            "fence_token": lock.get("fence_token", 0),
        }
    holder = holders[0] if holders else {}
    return {
        "mode": "mutex",
        "max_holders": 1,
        "lock_id": holder.get("lock_id") or lock["lock_id"],
        "holder_info": holder.get("holder_info", ""),
        "acquired_at": holder.get("acquired_at", 0),
        "expires_at": holder.get("expires_at", 0),
        "fence_token": lock.get("fence_token", 0),
    }


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
            kernel_locks = await asyncio.to_thread(
                nx.sys_readdir, "/__sys__/locks/", details=True, context=ctx
            )
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
            if locks or not _locks:
                return {"locks": locks, "count": len(locks)}
        except Exception as e:
            logger.debug("Kernel lock list failed: %s", e)

    # Fallback: in-memory store
    for k in list(_locks):
        _prune_expired_lock(k)

    locks = list(_locks.values())
    return {"locks": locks, "count": len(locks)}


@router.get("/{resource:path}")
async def get_lock_status(
    resource: str,
    _request: Request,
    _auth_result: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Return lock status for a resource path."""
    resource = _normalize_resource(resource)
    lock = _prune_expired_lock(resource)
    if lock:
        return {"path": resource, "locked": True, "lock_info": _status_info_from_lock(lock)}
    return {"path": resource, "locked": False, "lock_info": None}


async def _acquire_lock(
    *,
    resource: str,
    timeout: float,
    ttl: float,
    max_holders: int,
    blocking: bool,
    request: Request,
    auth_result: dict[str, Any],
) -> dict[str, Any]:
    """Acquire a lock through the kernel, with in-memory fallback/mirroring."""
    resource = _normalize_resource(resource)
    ctx = get_operation_context(auth_result)
    nx = _get_nexus_fs(request)

    existing = _prune_expired_lock(resource)
    if existing and (
        existing.get("mode") == "mutex"
        or existing.get("max_holders", 1) != max_holders
        or len(_lock_holders(existing)) >= max_holders
    ):
        raise HTTPException(status_code=409, detail=f"Resource already locked: {resource}")

    if nx and hasattr(nx, "sys_lock"):
        deadline = time.monotonic() + timeout
        try:
            while True:
                kernel_mode = "shared" if max_holders > 1 else "exclusive"
                lock_id = nx.sys_lock(
                    resource,
                    mode=kernel_mode,
                    ttl=ttl,
                    max_holders=max_holders,
                    context=ctx,
                )
                if lock_id:
                    now = time.time()
                    holder = {
                        "lock_id": lock_id,
                        "holder_info": getattr(ctx, "subject", "admin"),
                        "acquired_at": now,
                        "expires_at": now + ttl,
                    }
                    lock = _prune_expired_lock(resource) or {
                        "resource": resource,
                        "mode": _public_mode(max_holders),
                        "max_holders": max_holders,
                        "holders": [],
                        "ttl": ttl,
                        "fence_token": 0,
                    }
                    lock["holders"] = [*_lock_holders(lock), holder]
                    lock["lock_id"] = holder["lock_id"]
                    lock["holder_info"] = holder["holder_info"]
                    lock["acquired_at"] = holder["acquired_at"]
                    lock["expires_at"] = max(
                        float(h.get("expires_at", 0)) for h in _lock_holders(lock)
                    )
                    lock["ttl"] = ttl
                    _locks[resource] = lock
                    return _build_lock_response(lock, holder)
                if not blocking or time.monotonic() >= deadline:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Resource already locked: {resource}"
                            if blocking
                            else f"Resource already locked (non-blocking): {resource}"
                        ),
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except HTTPException:
            raise
        except PermissionDeniedError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.debug("Kernel lock acquire failed: %s", e)

    # Fallback: in-memory
    existing = _prune_expired_lock(resource)
    if existing and (
        existing.get("mode") == "mutex"
        or existing.get("max_holders", 1) != max_holders
        or len(_lock_holders(existing)) >= max_holders
    ):
        raise HTTPException(status_code=409, detail=f"Resource already locked: {resource}")

    now = time.time()
    lock_id = str(uuid.uuid4())
    holder = {
        "lock_id": lock_id,
        "holder_info": "admin",
        "acquired_at": now,
        "expires_at": now + ttl,
    }
    lock = existing or {
        "resource": resource,
        "mode": _public_mode(max_holders),
        "max_holders": max_holders,
        "holders": [],
        "ttl": ttl,
        "fence_token": 0,
    }
    lock["holders"] = [*_lock_holders(lock), holder]
    lock["lock_id"] = holder["lock_id"]
    lock["holder_info"] = holder["holder_info"]
    lock["acquired_at"] = holder["acquired_at"]
    lock["expires_at"] = max(float(h.get("expires_at", 0)) for h in _lock_holders(lock))
    lock["ttl"] = ttl
    _locks[resource] = lock
    return _build_lock_response(lock, holder)


@router.post("", status_code=status.HTTP_201_CREATED)
async def acquire_lock_legacy(
    body: LockAcquireRequest,
    request: Request,
    auth_result: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Acquire a lock using the documented body-based v2 API."""
    return await _acquire_lock(
        resource=body.path,
        timeout=body.timeout,
        ttl=body.ttl,
        max_holders=body.max_holders,
        blocking=body.blocking,
        request=request,
        auth_result=auth_result,
    )


@router.post("/{resource:path}/acquire")
async def acquire_lock(
    resource: str,
    body: PathAcquireRequest,
    request: Request,
    auth_result: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Acquire a lock on a resource path."""
    max_holders = 1 if body.mode == "mutex" else 2
    return await _acquire_lock(
        resource=resource,
        timeout=0,
        ttl=body.ttl_seconds,
        max_holders=max_holders,
        blocking=False,
        request=request,
        auth_result=auth_result,
    )


@router.delete("/{resource:path}")
async def release_lock(
    resource: str,
    lock_id: str,
    request: Request,
    force: bool = Query(False),
    auth_result: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Release a lock."""
    resource = _normalize_resource(resource)
    ctx = get_operation_context(auth_result)
    if force and not (getattr(ctx, "is_admin", False) or getattr(ctx, "is_system", False)):
        raise HTTPException(status_code=403, detail="force release requires admin")
    mirrored = _prune_expired_lock(resource)
    if mirrored and not force and _find_holder(mirrored, lock_id) is None:
        raise HTTPException(status_code=403, detail="Lock ID mismatch")
    nx = _get_nexus_fs(request)
    if nx and hasattr(nx, "sys_unlock"):
        try:
            released = bool(nx.sys_unlock(resource, lock_id=lock_id, force=force, context=ctx))
            if released and force:
                _locks.pop(resource, None)
            elif released and mirrored:
                holders = [h for h in _lock_holders(mirrored) if h.get("lock_id") != lock_id]
                if holders:
                    mirrored["holders"] = holders
                    mirrored["lock_id"] = holders[0].get("lock_id")
                    mirrored["holder_info"] = holders[0].get("holder_info", "")
                    mirrored["acquired_at"] = holders[0].get("acquired_at", 0)
                    mirrored["expires_at"] = max(float(h.get("expires_at", 0)) for h in holders)
                    _locks[resource] = mirrored
                else:
                    _locks.pop(resource, None)
            return {"released": released, "path": resource}
        except HTTPException:
            raise
        except PermissionDeniedError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.debug("Kernel lock release failed: %s", e)

    mirrored = _prune_expired_lock(resource)
    if not mirrored:
        raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")
    if not force and _find_holder(mirrored, lock_id) is None:
        raise HTTPException(status_code=403, detail="Lock ID mismatch")
    if force:
        del _locks[resource]
    else:
        holders = [h for h in _lock_holders(mirrored) if h.get("lock_id") != lock_id]
        if holders:
            mirrored["holders"] = holders
            mirrored["lock_id"] = holders[0].get("lock_id")
            mirrored["holder_info"] = holders[0].get("holder_info", "")
            mirrored["acquired_at"] = holders[0].get("acquired_at", 0)
            mirrored["expires_at"] = max(float(h.get("expires_at", 0)) for h in holders)
        else:
            del _locks[resource]
    return {"released": True, "path": resource}


@router.patch("/{resource:path}")
async def extend_lock(
    resource: str,
    body: LockExtendRequest,
    request: Request,
    auth_result: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Extend a lock's TTL."""
    resource = _normalize_resource(resource)
    ctx = get_operation_context(auth_result)
    mirrored = _prune_expired_lock(resource)
    if mirrored and _find_holder(mirrored, body.lock_id) is None:
        raise HTTPException(status_code=403, detail="Lock ID mismatch")
    nx = _get_nexus_fs(request)
    if nx and hasattr(nx, "sys_lock"):
        try:
            # sys_lock with existing lock_id = extend TTL
            result = nx.sys_lock(resource, lock_id=body.lock_id, ttl=body.ttl, context=ctx)
            if result:
                if mirrored is not None:
                    for mirrored_holder in _lock_holders(mirrored):
                        if mirrored_holder.get("lock_id") == body.lock_id:
                            mirrored_holder["expires_at"] = time.time() + body.ttl
                            break
                    mirrored["expires_at"] = max(
                        float(h.get("expires_at", 0)) for h in _lock_holders(mirrored)
                    )
                    mirrored["ttl"] = body.ttl
                return {
                    "status": "extended",
                    "resource": resource,
                    "path": resource,
                    "lock_id": body.lock_id,
                    "ttl": body.ttl,
                }
        except PermissionDeniedError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.debug("Kernel lock extend failed: %s", e)

    mirrored = _prune_expired_lock(resource)
    if not mirrored:
        raise HTTPException(status_code=404, detail=f"Lock not found: {resource}")
    holder = _find_holder(mirrored, body.lock_id)
    if holder is None:
        raise HTTPException(status_code=403, detail="Lock ID mismatch")
    holder["expires_at"] = time.time() + body.ttl
    mirrored["expires_at"] = max(float(h.get("expires_at", 0)) for h in _lock_holders(mirrored))
    mirrored["ttl"] = body.ttl
    return {"status": "extended", **mirrored, "path": resource}
