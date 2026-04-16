"""LockMixin — Advisory locking syscalls (POSIX flock equivalent).

Tier 1: sys_lock (acquire + extend), sys_unlock (release + force)

lock_list → sys_readdir("/__sys__/locks/")

Delegates to Rust kernel (PyKernel.sys_lock / sys_unlock).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class LockMixin:
    """Advisory locking: sys_lock / sys_unlock."""

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
        return self._kernel.sys_lock(
            path,
            lock_id=lock_id or "",
            mode=mode,
            max_holders=max_holders,
            ttl_secs=int(ttl),
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
        if not force and not lock_id:
            raise ValueError("lock_id is required for non-force release")
        return self._kernel.sys_unlock(path, lock_id=lock_id or "", force=force)

    # ── Distributed lock helpers (sync bridge for write(lock=True)) ──

    def _acquire_lock_sync(
        self,
        path: str,
        timeout: float,
        context: "OperationContext | None",  # noqa: ARG002
    ) -> str | None:
        """Acquire advisory lock synchronously via kernel sys_lock."""
        from nexus.contracts.exceptions import LockTimeout

        lock_id = self.sys_lock(path, ttl=timeout)

        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)

        return lock_id

    def _release_lock_sync(
        self,
        lock_id: str,
        path: str,
        context: "OperationContext | None",  # noqa: ARG002
    ) -> None:
        """Release advisory lock synchronously via kernel sys_unlock."""
        if not lock_id:
            return

        try:
            self.sys_unlock(path, lock_id=lock_id)
        except Exception as e:
            logger.error(f"Failed to release lock {lock_id} for {path}: {e}")
