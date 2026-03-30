"""LockMixin — Advisory locking syscalls (POSIX flock equivalent).

Tier 1: sys_lock, sys_unlock (single try-acquire / release)
Tier 2: lock_acquire, lock_info, lock_list, lock_extend, lock_force_release

Delegates to kernel AdvisoryLockManager (local or Raft-backed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.lib.distributed_lock import AdvisoryLockManager

logger = logging.getLogger(__name__)


class LockMixin:
    """Advisory locking: sys_lock / sys_unlock + Tier 2 helpers."""

    # Provided by NexusFS.__init__
    _lock_manager: "AdvisoryLockManager"

    def _validate_path(self, path: str, allow_root: bool = False) -> str:  # noqa: ARG002
        return path  # overridden by NexusFS

    # ── Locking (POSIX flock equivalent) ──────────────────────────

    @rpc_expose(description="Acquire advisory lock on a path")
    async def sys_lock(
        self,
        path: str,
        mode: str = "exclusive",
        ttl: float = 30.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> str | None:
        """Acquire advisory lock (POSIX flock(2)). Returns lock_id or None.

        Tier 1 syscall — single try-acquire, returns immediately.
        Use Tier 2 ``lock()`` for blocking wait with retry.
        """
        path = self._validate_path(path)
        _mode: Literal["exclusive", "shared"] = "exclusive" if mode == "exclusive" else "shared"
        return await self._lock_manager.acquire(
            path,
            mode=_mode,
            ttl=ttl,
            max_holders=max_holders,
            timeout=0,  # try-once, no blocking
        )

    @rpc_expose(description="Release advisory lock")
    async def sys_unlock(
        self,
        path: str,
        lock_id: str,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> bool:
        """Release advisory lock. Returns True if released."""
        path = self._validate_path(path)
        return await self._lock_manager.release(lock_id, path)

    @rpc_expose(description="Get advisory lock info for a path")
    async def lock_info(
        self,
        path: str,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Get lock info for path (Tier 2 admin query)."""
        path = self._validate_path(path)
        info = await self._lock_manager.get_lock_info(path)
        if info is None:
            return None
        return {
            "path": info.path,
            "mode": info.mode,
            "max_holders": info.max_holders,
            "fence_token": info.fence_token,
            "holders": [
                {
                    "lock_id": h.lock_id,
                    "holder_info": h.holder_info,
                    "acquired_at": h.acquired_at,
                    "expires_at": h.expires_at,
                }
                for h in info.holders
            ],
        }

    @rpc_expose(description="List active advisory locks")
    async def lock_list(
        self,
        pattern: str = "",
        limit: int = 100,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """List active advisory locks (Tier 2 admin query)."""
        locks = await self._lock_manager.list_locks(pattern=pattern, limit=limit)
        return {
            "locks": [await self.lock_info(lk.path) for lk in locks],
            "count": len(locks),
        }

    @rpc_expose(description="Extend advisory lock TTL (heartbeat)")
    async def lock_extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = 60.0,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Extend lock TTL / heartbeat (Tier 2)."""
        path = self._validate_path(path)
        result = await self._lock_manager.extend(lock_id, path, ttl=ttl)
        return {
            "success": result.success,
            "lock_info": (await self.lock_info(path)) if result.lock_info else None,
        }

    @rpc_expose(description="Acquire advisory lock (Tier 2: dict result for RPC)")
    async def lock_acquire(
        self,
        path: str,
        mode: str = "exclusive",
        ttl: float = 30.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Tier 2 wrapper over sys_lock — returns structured dict for RPC.

        sys_lock returns raw str|None which gRPC Call can't serialize.
        This wraps it in {"acquired": bool, "lock_id": str|None}.
        """
        lock_id = await self.sys_lock(
            path, mode=mode, ttl=ttl, max_holders=max_holders, context=context
        )
        return {"acquired": lock_id is not None, "lock_id": lock_id}

    @rpc_expose(description="Force-release all holders of a lock (admin)")
    async def lock_force_release(
        self,
        path: str,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Force-release all holders (Tier 2 admin operation)."""
        path = self._validate_path(path)
        released = await self._lock_manager.force_release(path)
        return {"released": released}

    # ── Distributed lock helpers (sync bridge for write(lock=True)) ──

    def _acquire_lock_sync(
        self,
        path: str,
        timeout: float,
        context: "OperationContext | None",  # noqa: ARG002
    ) -> str | None:
        """Acquire advisory lock synchronously via kernel _lock_manager.

        This method bridges sync write() with async lock operations.
        For async contexts, use `async with nx.locked()` instead.
        """
        import asyncio

        from nexus.contracts.exceptions import LockTimeout

        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "write(lock=True) cannot be used from async context (event loop detected). "
                "Use `async with nx.locked(path):` and `write(lock=False)` instead."
            )
        except RuntimeError as e:
            if "event loop detected" in str(e):
                raise

        async def acquire_lock() -> str | None:
            return await self._lock_manager.acquire(path=path, timeout=timeout)

        from nexus.lib.sync_bridge import run_sync

        lock_id = run_sync(acquire_lock())

        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)

        return lock_id

    def _release_lock_sync(
        self,
        lock_id: str,
        path: str,
        context: "OperationContext | None",  # noqa: ARG002
    ) -> None:
        """Release advisory lock synchronously via kernel _lock_manager."""
        if not lock_id:
            return

        async def release_lock() -> None:
            await self._lock_manager.release(lock_id, path)

        from nexus.lib.sync_bridge import run_sync

        try:
            run_sync(release_lock())
        except Exception as e:
            logger.error(f"Failed to release lock {lock_id} for {path}: {e}")
