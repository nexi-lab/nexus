"""LockMixin — Advisory locking syscalls (POSIX flock equivalent).

Tier 1: sys_lock (acquire + extend), sys_unlock (release + force)
Tier 2: lock_acquire (dict wrapper for RPC)

lock_info → sys_stat(include_lock=True)
lock_list → sys_readdir("/__sys__/locks/")

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

    # ── Tier 1: Locking (POSIX flock equivalent) ─────────────────

    @rpc_expose(description="Acquire or extend advisory lock on a path")
    def sys_lock(
        self,
        path: str,
        mode: str = "exclusive",
        ttl: float = 30.0,
        max_holders: int = 1,
        lock_id: str | None = None,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> str | None:
        """Acquire or extend advisory lock (POSIX fcntl(F_SETLK)).

        Tier 1 syscall — try-once semantics, returns immediately.

        When lock_id is None: try-acquire a new lock.
        When lock_id is provided: extend TTL of an existing lock (heartbeat).

        Returns lock_id on success, None on failure.
        """
        path = self._validate_path(path)
        _mode: Literal["exclusive", "shared"] = "exclusive" if mode == "exclusive" else "shared"

        if lock_id is not None:
            # Extend existing lock TTL (heartbeat)
            result = self._lock_manager.extend(lock_id, path, ttl=ttl)
            return lock_id if result.success else None

        # Try-acquire new lock
        return self._lock_manager.acquire(
            path,
            mode=_mode,
            ttl=ttl,
            max_holders=max_holders,
            timeout=0,  # try-once, no blocking
        )

    @rpc_expose(description="Release advisory lock (normal or force)")
    def sys_unlock(
        self,
        path: str,
        lock_id: str | None = None,
        force: bool = False,
        *,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> bool:
        """Release advisory lock.

        Tier 1 syscall.

        When force=False (default): release lock by lock_id (requires lock_id).
        When force=True: force-release ALL holders (admin operation, ignores lock_id).

        Returns True if released.
        """
        path = self._validate_path(path)
        if force:
            return self._lock_manager.force_release(path)
        if not lock_id:
            raise ValueError("lock_id is required for non-force release")
        return self._lock_manager.release(lock_id, path)

    # ── Tier 2: RPC-safe wrappers over Tier 1 ───────────────────

    @rpc_expose(description="Acquire advisory lock (blocking with timeout)")
    def lock_acquire(
        self,
        path: str,
        mode: str = "exclusive",
        ttl: float = 30.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Tier 2: wraps sys_lock with dict return for gRPC Call RPC.

        sys_lock returns raw str|None which gRPC encoder can't serialize.
        """
        lock_id = self.sys_lock(path, mode=mode, ttl=ttl, max_holders=max_holders, context=context)
        return {"acquired": lock_id is not None, "lock_id": lock_id}

    # ── Distributed lock helpers (sync bridge for write(lock=True)) ──

    def _acquire_lock_sync(
        self,
        path: str,
        timeout: float,
        context: "OperationContext | None",  # noqa: ARG002
    ) -> str | None:
        """Acquire advisory lock synchronously via kernel _lock_manager.

        Now that AdvisoryLockManager is fully sync, this is a direct call.
        Blocking wait handled by Rust Condvar (GIL released by PyO3).
        """
        from nexus.contracts.exceptions import LockTimeout

        lock_id = self._lock_manager.acquire(path=path, timeout=timeout)

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

        try:
            self._lock_manager.release(lock_id, path)
        except Exception as e:
            logger.error(f"Failed to release lock {lock_id} for {path}: {e}")
