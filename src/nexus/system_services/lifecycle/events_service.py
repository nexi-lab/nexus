"""Events Service — file watching and advisory locking.

Dual-track support:

Layer 1 (Same-box): OS-native file watching (inotify/FSEvents) + in-memory locks
Layer 2 (Distributed): EventBus (gRPC point-to-point / CacheStoreABC fan-out) + distributed locks

Per KERNEL-ARCHITECTURE.md §6 three-tier messaging:
  - System tier: gRPC IPC (point-to-point)
  - User Space tier: EventBus / CacheStoreABC (fan-out)

Phase 2: Core Refactoring (Issue #1287)
Extracted from: nexus_fs_events.py (836 lines)
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import validate_path
from nexus.core.protocols.connector import PassthroughProtocol
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.bricks.watch.file_watcher import FileWatcher
    from nexus.contracts.types import OperationContext
    from nexus.core.protocols.connector import ConnectorProtocol
    from nexus.lib.distributed_lock import LockManagerBase
    from nexus.system_services.event_subsystem.bus.base import EventBusBase


class EventsService:
    """Independent events service extracted from NexusFS.

    Provides dual-track support for file watching and locking:
    - Layer 1: Same-box mode using OS-native APIs (PassthroughBackend only)
    - Layer 2: Distributed mode using EventBus + distributed locks (any backend)

    Architecture:
        - Clean dependency injection for all external systems
        - No direct NexusFS access required
        - Dual-track selection based on available infrastructure
    """

    def __init__(
        self,
        backend: "ConnectorProtocol",
        event_bus: "EventBusBase | None" = None,
        lock_manager: "LockManagerBase | None" = None,
        zone_id: str | None = None,
    ):
        """Initialize events service.

        Args:
            backend: Storage backend (needed for same-box detection)
            event_bus: Distributed event bus (EventBus) or None
            lock_manager: Distributed lock manager or None
            zone_id: Default zone ID
        """
        self._backend = backend
        self._event_bus = event_bus
        self._lock_manager = lock_manager
        self._file_watcher: FileWatcher | None = None  # lazy init in _get_file_watcher()
        self._zone_id = zone_id
        self._event_tasks: set[asyncio.Task[Any]] = set()

        logger.info("[EventsService] Initialized")

    # =========================================================================
    # Infrastructure Detection
    # =========================================================================

    def _is_same_box(self) -> bool:
        """Check if we're in same-box mode (local file watching available)."""
        return self._backend.is_passthrough is True

    def _has_distributed_events(self) -> bool:
        """Check if distributed event bus is available."""
        return self._event_bus is not None

    def _has_distributed_locks(self) -> bool:
        """Check if distributed lock manager is available."""
        return self._lock_manager is not None

    def _get_file_watcher(self) -> "FileWatcher":
        """Get or create the file watcher instance for same-box mode."""
        if not self._is_same_box():
            raise NotImplementedError(
                "File watching is only available with PassthroughBackend (same-box mode). "
                "For distributed scenarios, configure Redis for RedisEventBus."
            )

        if self._file_watcher is None:
            import importlib as _il

            FileWatcher = _il.import_module("nexus.bricks.watch.file_watcher").FileWatcher
            self._file_watcher = FileWatcher()

        return self._file_watcher

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
        """Start listening for events from other instances to invalidate local cache.

        No-op placeholder — cache invalidation is handled by the event bus
        subscription in the distributed event path.  Provided so that
        multi-instance test fixtures can call it without AttributeError.
        """
        logger.debug("[EventsService] _start_cache_invalidation (no-op)")

    def _stop_cache_invalidation(self) -> None:
        """Stop cache invalidation listener.

        No-op placeholder — see ``_start_cache_invalidation``.
        """
        logger.debug("[EventsService] _stop_cache_invalidation (no-op)")

    # =========================================================================
    # System Readiness
    # =========================================================================

    async def _ensure_distributed_system_ready(self) -> None:
        """Ensure the distributed event system is ready for use."""
        if self._has_distributed_events() and not getattr(self._event_bus, "_started", False):
            try:
                await self._event_bus.start()  # type: ignore[union-attr]
                logger.debug("Event bus auto-started")
            except Exception as e:
                logger.warning("Failed to auto-start event bus: %s", e)

    # =========================================================================
    # Public API: File Watching
    # =========================================================================

    @rpc_expose(description="Wait for file system changes")
    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
        _context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Wait for file system changes on a path.

        Dual-track implementation:
        - Layer 2 (preferred): Uses EventBus (distributed)
        - Layer 1 (fallback): Uses OS-native file watching (same-box only)

        Args:
            path: Virtual path to watch
            timeout: Maximum time to wait in seconds (default: 30.0)
            since_revision: Only return events with revision > this value
            _context: Operation context (optional)

        Returns:
            Dict with change info if change detected, None if timeout
        """
        await self._ensure_distributed_system_ready()

        path = validate_path(path, allow_root=True)
        zone_id = self._get_zone_id(_context)

        # Layer 2: Distributed event bus (preferred)
        if self._has_distributed_events():
            logger.debug("Using distributed event bus for %s", path)
            event = await self._event_bus.wait_for_event(  # type: ignore[union-attr]
                zone_id=zone_id,
                path_pattern=path,
                timeout=timeout,
                since_revision=since_revision,
            )
            if event is None:
                return None
            return cast(dict[str, Any], event.to_dict())

        # Layer 1: Same-box local watching (fallback)
        if self._is_same_box():
            logger.debug("Using same-box file watcher for %s", path)
            from nexus.system_services.event_subsystem.types import FileEvent

            assert isinstance(self._backend, PassthroughProtocol), (
                "Backend must implement PassthroughProtocol for this operation"
            )
            pt_backend = self._backend

            watch_path = path.rstrip("/")
            if "*" in path or "?" in path:
                import os

                watch_path = os.path.dirname(watch_path.split("*")[0].split("?")[0])
                if not watch_path:
                    watch_path = "/"

            physical_path = pt_backend.get_physical_path(watch_path)
            watcher = self._get_file_watcher()

            is_pattern = "*" in path or "?" in path or path.endswith("/")
            deadline = None
            if is_pattern:
                deadline = asyncio.get_running_loop().time() + timeout

            while True:
                remaining_timeout = timeout
                if deadline is not None:
                    remaining_timeout = max(0, deadline - asyncio.get_running_loop().time())
                    if remaining_timeout <= 0:
                        return None

                change = await watcher.wait_for_change(physical_path, timeout=remaining_timeout)
                if change is None:
                    return None

                event = FileEvent.from_file_change(change, zone_id=zone_id)

                if is_pattern:
                    virtual_change_path = change.path
                    if not virtual_change_path.startswith("/"):
                        virtual_change_path = f"{watch_path.rstrip('/')}/{virtual_change_path}"
                    event = dataclasses.replace(event, path=virtual_change_path)
                    if event.matches_path_pattern(path):
                        result: dict[str, Any] = event.to_dict()
                        return result
                    logger.debug(
                        "Event %s didn't match pattern %s, continuing...", event.path, path
                    )
                    continue

                result_dict: dict[str, Any] = event.to_dict()
                return result_dict

        raise NotImplementedError(
            "No event source available. Either configure Redis for distributed events "
            "or use PassthroughBackend for same-box file watching."
        )

    # =========================================================================
    # Public API: Advisory Locking
    # =========================================================================

    @rpc_expose(description="Acquire advisory lock on a path")
    async def lock(
        self,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
        _context: "OperationContext | None" = None,
    ) -> str | None:
        """Acquire an advisory lock on a path.

        Supports both mutex (max_holders=1) and semaphore (max_holders>1) modes.

        Args:
            path: Virtual path to lock
            timeout: Maximum time to wait for lock in seconds
            ttl: Lock TTL in seconds (distributed mode only)
            max_holders: Maximum concurrent holders (1 = mutex)
            _context: Operation context (optional)

        Returns:
            Lock ID if acquired, None if timeout
        """
        await self._ensure_distributed_system_ready()

        path = validate_path(path, allow_root=True)
        zone_id = self._get_zone_id(_context)

        # Layer 2: Distributed lock manager (preferred)
        if self._has_distributed_locks():
            mode = "mutex" if max_holders == 1 else f"semaphore({max_holders})"
            logger.debug("Using distributed lock manager for %s (%s)", path, mode)
            lock_id = await self._lock_manager.acquire(  # type: ignore[union-attr]
                zone_id=zone_id,
                path=path,
                timeout=timeout,
                ttl=ttl,
                max_holders=max_holders,
            )
            if lock_id:
                logger.debug("Distributed lock acquired on %s: %s", path, lock_id)
            else:
                logger.warning("Distributed lock timeout on %s after %ss", path, timeout)
            return lock_id

        # Layer 1: Same-box in-memory locking (fallback)
        if self._is_same_box():
            mode = "mutex" if max_holders == 1 else f"semaphore({max_holders})"
            logger.debug("Using same-box lock for %s (%s)", path, mode)
            assert isinstance(self._backend, PassthroughProtocol), (
                "Backend must implement PassthroughProtocol for this operation"
            )
            pt_backend = self._backend
            lock_id = pt_backend.lock(path, timeout=timeout, max_holders=max_holders)

            if lock_id:
                logger.debug("Same-box lock acquired on %s: %s", path, lock_id)
            else:
                logger.warning("Same-box lock timeout on %s after %ss", path, timeout)
            return lock_id

        raise NotImplementedError(
            "No lock manager available. Either configure Redis for distributed locks "
            "or use PassthroughBackend for same-box locking."
        )

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
        zone_id = self._get_zone_id(_context)

        # Layer 2: Distributed lock manager
        if self._has_distributed_locks():
            path = validate_path(path, allow_root=True)
            extended = await self._lock_manager.extend(  # type: ignore[union-attr]
                lock_id=lock_id,
                zone_id=zone_id,
                path=path,
                ttl=ttl,
            )
            if extended.success:
                logger.debug("Lock extended: %s (TTL: %ss)", lock_id, ttl)
            else:
                logger.warning("Lock extend failed (not owned or expired): %s", lock_id)
            return extended.success

        # Layer 1: Same-box locks don't need extension (no TTL)
        if self._is_same_box():
            logger.debug("Same-box lock extend (no-op): %s", lock_id)
            return True

        raise NotImplementedError(
            "No lock manager available. Either configure Redis for distributed locks "
            "or use PassthroughBackend for same-box locking."
        )

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
            path: Path that was locked (required for distributed mode)
            _context: Operation context (optional)

        Returns:
            True if lock was released, False if not found
        """
        zone_id = self._get_zone_id(_context)

        # Layer 2: Distributed lock manager
        if self._has_distributed_locks():
            if path is None:
                raise ValueError("path is required for distributed unlock")
            path = validate_path(path, allow_root=True)
            released = await self._lock_manager.release(  # type: ignore[union-attr]
                lock_id=lock_id,
                zone_id=zone_id,
                path=path,
            )
            if released:
                logger.debug("Distributed lock released: %s", lock_id)
            else:
                logger.warning("Distributed lock not found: %s", lock_id)
            return released

        # Layer 1: Same-box in-memory locking
        if self._is_same_box():
            assert isinstance(self._backend, PassthroughProtocol), (
                "Backend must implement PassthroughProtocol for this operation"
            )
            pt_backend = self._backend
            released = pt_backend.unlock(lock_id)
            if released:
                logger.debug("Same-box lock released: %s", lock_id)
            else:
                logger.warning("Same-box lock not found: %s", lock_id)
            return released

        raise NotImplementedError(
            "No lock manager available. Either configure Redis for distributed locks "
            "or use PassthroughBackend for same-box locking."
        )

    # =========================================================================
    # Lock Context Manager
    # =========================================================================

    @contextlib.asynccontextmanager
    async def locked(
        self,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0,
        _context: "OperationContext | None" = None,
    ) -> AsyncIterator[str]:
        """Acquire a distributed lock as an async context manager.

        Args:
            path: Virtual path to lock
            timeout: Maximum time to wait for lock in seconds
            ttl: Lock TTL in seconds
            _context: Operation context (optional)

        Yields:
            lock_id: Lock identifier

        Raises:
            LockTimeout: If lock cannot be acquired within timeout
        """
        from nexus.contracts.exceptions import LockTimeout

        lock_id = await self.lock(path, timeout=timeout, ttl=ttl, _context=_context)
        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)

        try:
            yield lock_id
        finally:
            await self.unlock(lock_id, path, _context=_context)
