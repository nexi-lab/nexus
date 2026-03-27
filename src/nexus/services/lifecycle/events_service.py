"""Events Service — file watching RPC wrapper + advisory locking.

Thin service-layer wrapper around kernel FileWatcher (§4.5).
Exposes ``wait_for_changes()`` via RPC and manages advisory locks.

Architecture:
    - File watching delegated to kernel FileWatcher (local OBSERVE + optional remote)
    - Advisory locking (flock-style) managed here (service-tier concern)
    - ``@rpc_expose`` methods are the only service-layer additions

Phase 2: Core Refactoring (Issue #1287)
Extracted from: nexus_fs_events.py (836 lines)
"""

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.file_watcher import FileWatcher
    from nexus.lib.distributed_lock import AdvisoryLockManager


class EventsService:
    """Events service — RPC wrapper for kernel FileWatcher + advisory locking.

    File watching is fully delegated to the kernel FileWatcher primitive.
    This service adds:
    - ``@rpc_expose`` for gRPC/HTTP access
    - Advisory locking (lock/unlock/extend_lock/locked)
    - Zone ID resolution from OperationContext
    """

    def __init__(
        self,
        file_watcher: "FileWatcher",
        zone_id: str | None = None,
    ):
        self._file_watcher = file_watcher
        # Always create LocalLockManager — may be upgraded to RaftLockManager
        # at link time via upgrade_lock_manager().
        try:
            from nexus.lib.distributed_lock import LocalLockManager
            from nexus.lib.semaphore import create_vfs_semaphore

            self._lock_manager: "AdvisoryLockManager | None" = LocalLockManager(
                create_vfs_semaphore(), zone_id=zone_id or ROOT_ZONE_ID
            )
            logger.debug("[EventsService] LocalLockManager created")
        except Exception as exc:
            logger.debug("[EventsService] LocalLockManager unavailable: %s", exc)
            self._lock_manager = None
        self._zone_id = zone_id

        logger.info("[EventsService] Initialized (delegates to kernel FileWatcher)")

    # =========================================================================
    # Infrastructure Detection
    # =========================================================================

    def _has_lock_manager(self) -> bool:
        """Check if advisory lock manager is available."""
        return self._lock_manager is not None

    def upgrade_lock_manager(self, lock_manager: "AdvisoryLockManager") -> None:
        """Hot-swap LocalLockManager → RaftLockManager at link time.

        Safe because this runs during _do_link(), before bootstrap/serve —
        no concurrent access to ``self._lock_manager``.
        """
        logger.info(
            "[EventsService] Lock manager upgraded: %s → %s",
            type(self._lock_manager).__name__,
            type(lock_manager).__name__,
        )
        self._lock_manager = lock_manager

    def _get_zone_id(self, context: "OperationContext | None") -> str:
        """Get zone ID from context or default."""
        if context and hasattr(context, "zone_id") and context.zone_id:
            return context.zone_id
        if self._zone_id:
            return self._zone_id
        return ROOT_ZONE_ID

    # =========================================================================
    # Cache Invalidation Hooks (used by multi-instance tests)
    # =========================================================================

    def _start_cache_invalidation(self) -> None:
        """No-op placeholder for multi-instance test fixtures."""
        logger.debug("[EventsService] _start_cache_invalidation (no-op)")

    def _stop_cache_invalidation(self) -> None:
        """No-op placeholder — see ``_start_cache_invalidation``."""
        logger.debug("[EventsService] _stop_cache_invalidation (no-op)")

    # =========================================================================
    # Public API: File Watching
    # =========================================================================

    @rpc_expose(description="Wait for file system changes")
    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
        _context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Wait for file system changes on a path.

        Delegates to kernel FileWatcher which races local OBSERVE (~0µs)
        and optional remote watcher (distributed) via FIRST_COMPLETED.

        Args:
            path: Virtual path to watch (supports glob patterns)
            timeout: Maximum time to wait in seconds (default: 30.0)
            _context: Operation context (optional)

        Returns:
            Dict with change info if change detected, None if timeout
        """
        path = validate_path(path, allow_root=True)
        zone_id = self._get_zone_id(_context)

        event = await self._file_watcher.wait(path, timeout=timeout, zone_id=zone_id)
        if event is None:
            return None
        return event.to_dict()

    # =========================================================================
    # Public API: Advisory Locking
    # =========================================================================

    @rpc_expose(description="Acquire advisory lock on a path")
    async def lock(
        self,
        path: str,
        mode: Literal["exclusive", "shared"] = "exclusive",
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
        _context: "OperationContext | None" = None,
    ) -> str | None:
        """Acquire an advisory lock on a path.

        Supports exclusive (default), shared, and counting semaphore modes.

        Args:
            path: Virtual path to lock
            mode: ``"exclusive"`` (default) or ``"shared"``
            timeout: Maximum time to wait for lock in seconds
            ttl: Lock TTL in seconds
            max_holders: Maximum concurrent holders (1 = mutex)
            _context: Operation context (optional)

        Returns:
            Lock ID if acquired, None if timeout
        """
        path = validate_path(path, allow_root=True)

        if not self._has_lock_manager():
            raise RuntimeError(
                "No lock manager available. EventsService should always have a lock "
                "manager (local fallback or distributed)."
            )

        desc = f"mode={mode}" if max_holders == 1 else f"semaphore({max_holders})"
        logger.debug("Acquiring lock on %s (%s)", path, desc)
        lock_id = await self._lock_manager.acquire(  # type: ignore[union-attr]
            path=path,
            mode=mode,
            timeout=timeout,
            ttl=ttl,
            max_holders=max_holders,
        )
        if lock_id:
            logger.debug("Lock acquired on %s: %s", path, lock_id)
        else:
            logger.warning("Lock timeout on %s after %ss", path, timeout)
        return lock_id

    @rpc_expose(description="Extend lock TTL (heartbeat)")
    async def extend_lock(
        self,
        lock_id: str,
        path: str,
        ttl: float = 30.0,
        _context: "OperationContext | None" = None,
    ) -> bool:
        """Extend a lock's TTL (heartbeat for long-running operations).

        Args:
            lock_id: Lock ID returned from lock()
            path: Path that was locked
            ttl: New TTL in seconds
            _context: Operation context (optional)

        Returns:
            True if lock was extended, False if not found/owned
        """
        if not self._has_lock_manager():
            raise RuntimeError("No lock manager available.")

        path = validate_path(path, allow_root=True)
        extended = await self._lock_manager.extend(  # type: ignore[union-attr]
            lock_id=lock_id,
            path=path,
            ttl=ttl,
        )
        if extended.success:
            logger.debug("Lock extended: %s (TTL: %ss)", lock_id, ttl)
        else:
            logger.warning("Lock extend failed (not owned or expired): %s", lock_id)
        return extended.success

    @rpc_expose(description="Release advisory lock")
    async def unlock(
        self,
        lock_id: str,
        path: str | None = None,
        _context: "OperationContext | None" = None,
    ) -> bool:
        """Release an advisory lock.

        Args:
            lock_id: Lock ID returned from lock()
            path: Path that was locked (required)
            _context: Operation context (optional)

        Returns:
            True if lock was released, False if not found
        """
        if not self._has_lock_manager():
            raise RuntimeError("No lock manager available.")

        if path is None:
            raise ValueError("path is required for unlock")
        path = validate_path(path, allow_root=True)
        released = await self._lock_manager.release(  # type: ignore[union-attr]
            lock_id=lock_id,
            path=path,
        )
        if released:
            logger.debug("Lock released: %s", lock_id)
        else:
            logger.warning("Lock not found: %s", lock_id)
        return released

    # =========================================================================
    # Lock Context Manager
    # =========================================================================

    @contextlib.asynccontextmanager
    async def locked(
        self,
        path: str,
        mode: Literal["exclusive", "shared"] = "exclusive",
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
        _context: "OperationContext | None" = None,
    ) -> AsyncIterator[str]:
        """Acquire an advisory lock as an async context manager.

        Args:
            path: Virtual path to lock
            mode: ``"exclusive"`` (default) or ``"shared"``
            timeout: Maximum time to wait for lock in seconds
            ttl: Lock TTL in seconds
            max_holders: Maximum concurrent holders (1 = mutex)
            _context: Operation context (optional)

        Yields:
            lock_id: Lock identifier

        Raises:
            LockTimeout: If lock cannot be acquired within timeout
        """
        from nexus.contracts.exceptions import LockTimeout

        lock_id = await self.lock(
            path,
            mode=mode,
            timeout=timeout,
            ttl=ttl,
            max_holders=max_holders,
            _context=_context,
        )
        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)

        try:
            yield lock_id
        finally:
            await self.unlock(lock_id, path, _context=_context)
