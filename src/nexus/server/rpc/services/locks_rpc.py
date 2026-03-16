"""Locks RPC Service — list, info, release distributed locks.

Issue #1520.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class LocksRPCService:
    """RPC surface for distributed lock operations."""

    def __init__(self, lock_manager: Any) -> None:
        self._lock_manager = lock_manager

    @rpc_expose(description="List active locks")
    async def lock_list(self, zone_id: str | None = None) -> dict[str, Any]:
        locks = await self._lock_manager.list_locks(zone_id=zone_id)
        return {
            "locks": [self._lock_to_dict(lk) for lk in locks],
            "count": len(locks),
        }

    @rpc_expose(description="Get lock status for a path")
    async def lock_info(self, path: str) -> dict[str, Any]:
        info = await self._lock_manager.get_lock_info(path=path)
        if info is None:
            return {"locked": False, "lock_info": None}
        return {"locked": True, "lock_info": self._lock_to_dict(info)}

    @rpc_expose(description="Release a lock")
    async def lock_release(
        self,
        path: str,
        lock_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if force:
            await self._lock_manager.force_release(path=path)
            return {"released": True, "forced": True}
        if not lock_id:
            raise ValueError("lock_id is required for non-force release")
        await self._lock_manager.release(lock_id=lock_id, path=path)
        return {"released": True}

    @staticmethod
    def _lock_to_dict(lock_info: Any) -> dict[str, Any]:
        """Convert a LockInfo object to a serialisable dict."""
        if isinstance(lock_info, dict):
            return lock_info
        return {
            k: str(v) if v is not None else None
            for k, v in (
                lock_info.__dict__ if hasattr(lock_info, "__dict__") else {"value": lock_info}
            ).items()
        }
