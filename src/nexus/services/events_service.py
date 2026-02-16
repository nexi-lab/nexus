"""Events Service - Extracted from NexusFSEventsMixin.

This service handles file watching and advisory locking operations
with dual-track support:

Layer 1 (Same-box): OS-native file watching (inotify/FSEvents) + in-memory locks
Layer 2 (Distributed): Redis Pub/Sub events + distributed locks

Phase 2: Core Refactoring (Issue #1287)
Extracted from: nexus_fs_events.py (836 lines)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from nexus.core.path_utils import validate_path
from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.distributed_lock import LockManagerBase
    from nexus.core.event_bus import EventBusBase
    from nexus.core.file_watcher import FileWatcher
    from nexus.core.permissions import OperationContext


class EventsService:
    """Independent events service extracted from NexusFS.

    Provides dual-track support for file watching and locking:
    - Layer 1: Same-box mode using OS-native APIs (PassthroughBackend only)
    - Layer 2: Distributed mode using Redis Pub/Sub and locks (any backend)

    Architecture:
        - Clean dependency injection for all external systems
        - No direct NexusFS access required
        - Dual-track selection based on available infrastructure
    """

    def __init__(
        self,
        backend: Backend,
        event_bus: EventBusBase | None = None,
        lock_manager: LockManagerBase | None = None,
        file_watcher: FileWatcher | None = None,
        zone_id: str | None = None,
        metadata_cache: Any = None,
    ):
        """Initialize events service.

        Args:
            backend: Storage backend (needed for same-box detection)
            event_bus: Distributed event bus (Redis Pub/Sub) or None
            lock_manager: Distributed lock manager or None
            file_watcher: OS-native file watcher or None (lazy init for same-box)
            zone_id: Default zone ID
            metadata_cache: Metadata cache instance for invalidation
        """
        self._backend = backend
        self._event_bus = event_bus
        self._lock_manager = lock_manager
        self._file_watcher = file_watcher
        self._zone_id = zone_id
        self._metadata_cache = metadata_cache
        self._cache_invalidation_started = False
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

    def _get_file_watcher(self) -> FileWatcher:
        """Get or create the file watcher instance for same-box mode."""
        if not self._is_same_box():
            raise NotImplementedError(
                "File watching is only available with PassthroughBackend (same-box mode). "
                "For distributed scenarios, configure Redis for GlobalEventBus."
            )

        if self._file_watcher is None:
            from nexus.core.file_watcher import FileWatcher

            self._file_watcher = FileWatcher()

        return self._file_watcher

    def _get_zone_id(self, context: OperationContext | None) -> str:
        """Get zone ID from context or default."""
        if context and hasattr(context, "zone_id") and context.zone_id:
            return context.zone_id
        if self._zone_id:
            return self._zone_id
        return "default"

    # =========================================================================
    # System Readiness
    # =========================================================================

    def _should_auto_start_cache_invalidation(self) -> bool:
        """Check if cache invalidation should be auto-started."""
        if self._cache_invalidation_started:
            return False
        if self._metadata_cache is None:
            return False
        has_event_source = self._has_distributed_events() or self._is_same_box()
        return has_event_source

    async def _ensure_distributed_system_ready(self) -> None:
        """Ensure the distributed event system is ready for use."""
        if self._has_distributed_events() and not getattr(self._event_bus, "_started", False):
            try:
                await self._event_bus.start()  # type: ignore[union-attr]
                logger.debug("Event bus auto-started")
            except Exception as e:
                logger.warning(f"Failed to auto-start event bus: {e}")

        if self._should_auto_start_cache_invalidation():
            self._start_cache_invalidation()
            logger.info("Cache invalidation auto-started (caching enabled with event source)")

    # =========================================================================
    # Public API: File Watching
    # =========================================================================

    @rpc_expose(description="Wait for file system changes")
    async def wait_for_changes(
        self,
        path: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Wait for file system changes on a path.

        Dual-track implementation:
        - Layer 2 (preferred): Uses GlobalEventBus (Redis Pub/Sub)
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
            logger.debug(f"Using distributed event bus for {path}")
            event = await self._event_bus.wait_for_event(  # type: ignore[union-attr]
                zone_id=zone_id,
                path_pattern=path,
                timeout=timeout,
                since_revision=since_revision,
            )
            if event is None:
                return None
            return event.to_dict()

        # Layer 1: Same-box local watching (fallback)
        if self._is_same_box():
            logger.debug(f"Using same-box file watcher for {path}")
            from nexus.core.event_bus import FileEvent

            assert self._backend.is_passthrough, "Backend must be passthrough for this operation"

            watch_path = path.rstrip("/")
            if "*" in path or "?" in path:
                import os

                watch_path = os.path.dirname(watch_path.split("*")[0].split("?")[0])
                if not watch_path:
                    watch_path = "/"

            physical_path = self._backend.get_physical_path(watch_path)
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
                    event.path = virtual_change_path
                    if event.matches_path_pattern(path):
                        return event.to_dict()
                    logger.debug(f"Event {event.path} didn't match pattern {path}, continuing...")
                    continue

                return event.to_dict()

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
        _context: OperationContext | None = None,
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
            logger.debug(f"Using distributed lock manager for {path} ({mode})")
            lock_id = await self._lock_manager.acquire(  # type: ignore[union-attr]
                zone_id=zone_id,
                path=path,
                timeout=timeout,
                ttl=ttl,
                max_holders=max_holders,
            )
            if lock_id:
                logger.debug(f"Distributed lock acquired on {path}: {lock_id}")
            else:
                logger.warning(f"Distributed lock timeout on {path} after {timeout}s")
            return lock_id

        # Layer 1: Same-box in-memory locking (fallback)
        if self._is_same_box():
            mode = "mutex" if max_holders == 1 else f"semaphore({max_holders})"
            logger.debug(f"Using same-box lock for {path} ({mode})")
            assert self._backend.is_passthrough, "Backend must be passthrough for this operation"
            lock_id = self._backend.lock(path, timeout=timeout, max_holders=max_holders)

            if lock_id:
                logger.debug(f"Same-box lock acquired on {path}: {lock_id}")
            else:
                logger.warning(f"Same-box lock timeout on {path} after {timeout}s")
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
        _context: OperationContext | None = None,
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
                logger.debug(f"Lock extended: {lock_id} (TTL: {ttl}s)")
            else:
                logger.warning(f"Lock extend failed (not owned or expired): {lock_id}")
            return extended.success

        # Layer 1: Same-box locks don't need extension (no TTL)
        if self._is_same_box():
            logger.debug(f"Same-box lock extend (no-op): {lock_id}")
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
        _context: OperationContext | None = None,
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
                logger.debug(f"Distributed lock released: {lock_id}")
            else:
                logger.warning(f"Distributed lock not found: {lock_id}")
            return released

        # Layer 1: Same-box in-memory locking
        if self._is_same_box():
            assert self._backend.is_passthrough, "Backend must be passthrough for this operation"
            released = self._backend.unlock(lock_id)
            if released:
                logger.debug(f"Same-box lock released: {lock_id}")
            else:
                logger.warning(f"Same-box lock not found: {lock_id}")
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
        _context: OperationContext | None = None,
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
        from nexus.core.exceptions import LockTimeout

        lock_id = await self.lock(path, timeout=timeout, ttl=ttl, _context=_context)
        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)

        try:
            yield lock_id
        finally:
            await self.unlock(lock_id, path, _context=_context)

    # =========================================================================
    # Cache Invalidation
    # =========================================================================

    def _invalidate_cache_for_path(self, path: str) -> None:
        """Invalidate metadata cache for a path."""
        if self._metadata_cache is not None:
            virtual_path = path
            if self._is_same_box():
                assert self._backend.is_passthrough, "Backend must be passthrough for this operation"
                base_path = str(self._backend.base_path)
                if path.startswith(base_path):
                    virtual_path = path[len(base_path) :]
                    if not virtual_path.startswith("/"):
                        virtual_path = "/" + virtual_path

            self._metadata_cache.invalidate_path(virtual_path)
            logger.debug(f"Cache invalidated: {virtual_path}")

    def _handle_cache_invalidation_event(
        self, event_type: Any, path: str, old_path: str | None
    ) -> None:
        """Handle cache invalidation for any event source."""
        from nexus.core.event_bus import FileEventType

        if isinstance(event_type, str):
            try:
                event_type = FileEventType(event_type)
            except ValueError:
                logger.debug(f"Unknown event type: {event_type}")
                return

        if event_type in (
            FileEventType.FILE_WRITE,
            FileEventType.FILE_DELETE,
            FileEventType.DIR_CREATE,
            FileEventType.DIR_DELETE,
        ):
            self._invalidate_cache_for_path(path)
        elif event_type == FileEventType.FILE_RENAME:
            self._invalidate_cache_for_path(path)
            if old_path:
                self._invalidate_cache_for_path(old_path)

    def _on_file_change(self, change: Any) -> None:
        """Callback for Layer 1 (FileWatcher) file change events."""
        from nexus.core.event_bus import FileEvent

        event = FileEvent.from_file_change(change)
        self._handle_cache_invalidation_event(event.type, change.path, change.old_path)

    def _on_distributed_event(self, event: Any) -> None:
        """Callback for Layer 2 (EventBus) distributed events."""
        self._handle_cache_invalidation_event(event.type, event.path, event.old_path)

    async def _async_handle_event(self, event: Any) -> None:
        """Async wrapper for handling events during startup sync."""
        self._on_distributed_event(event)

    def _start_cache_invalidation(self) -> None:
        """Start cache invalidation listeners for both Layer 1 and Layer 2."""
        if self._cache_invalidation_started:
            logger.debug("Cache invalidation already started, skipping")
            return

        self._cache_invalidation_started = True

        # Layer 2: Distributed event bus (Redis Pub/Sub)
        if self._has_distributed_events():
            zone_id = self._get_zone_id(None)

            async def _subscribe_loop() -> None:
                try:
                    if hasattr(self._event_bus, "startup_sync"):
                        try:
                            synced = await self._event_bus.startup_sync(  # type: ignore[union-attr]
                                event_handler=self._async_handle_event,
                            )
                            if synced > 0:
                                logger.info(f"Startup sync: processed {synced} missed events")
                        except Exception as e:
                            logger.warning(f"Startup sync failed (continuing anyway): {e}")

                    logger.info(f"Starting distributed cache invalidation for zone: {zone_id}")
                    assert self._event_bus is not None
                    async for event in self._event_bus.subscribe(zone_id):
                        self._on_distributed_event(event)
                except asyncio.CancelledError:
                    logger.info("Distributed cache invalidation stopped")
                    raise
                except Exception as e:
                    logger.error(f"Distributed cache invalidation error: {e}")

            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(_subscribe_loop())
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)
            except RuntimeError:
                logger.debug("No running event loop, skipping distributed cache invalidation")

        # Layer 1: Same-box file watching (OS-native callbacks)
        if self._is_same_box():
            assert self._backend.is_passthrough, "Backend must be passthrough for this operation"
            try:
                watcher = self._get_file_watcher()

                if not watcher._started:
                    try:
                        loop = asyncio.get_running_loop()
                        watcher.start(loop)
                    except RuntimeError:
                        watcher.start()

                root_path = self._backend.base_path
                watcher.add_watch(root_path, self._on_file_change, recursive=True)
                logger.info(f"Started same-box cache invalidation: {root_path}")
            except Exception as e:
                logger.warning(f"Could not start same-box cache invalidation: {e}")

    def _stop_cache_invalidation(self) -> None:
        """Stop all cache invalidation listeners."""
        for task in list(self._event_tasks):
            if not task.done():
                task.cancel()

        if self._file_watcher is not None:
            self._file_watcher.stop()

        self._cache_invalidation_started = False
        logger.debug("Cache invalidation stopped")
