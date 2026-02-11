"""Event operations for NexusFS.

This module provides file watching and locking operations with dual-track support:

Layer 1 (Same-box, Block 1):
- Uses OS-native APIs (inotify on Linux, ReadDirectoryChangesW on Windows)
- Only works with PassthroughBackend
- In-memory advisory locks

Layer 2 (Distributed, Block 2):
- Uses Redis Pub/Sub for events via GlobalEventBus
- Uses Redis SET NX EX for locks via DistributedLockManager
- Works across multiple Nexus nodes

Selection Logic:
1. If GlobalEventBus/DistributedLockManager is available → use distributed (Layer 2)
2. Else if PassthroughBackend → use local (Layer 1)
3. Else → raise NotImplementedError
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.distributed_lock import LockManagerBase
    from nexus.core.event_bus import EventBusBase
    from nexus.core.file_watcher import FileWatcher
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class NexusFSEventsMixin:
    """Mixin providing event operations for NexusFS.

    Provides dual-track support for file watching and locking:
    - Layer 1: Same-box mode using OS-native APIs (PassthroughBackend only)
    - Layer 2: Distributed mode using Redis Pub/Sub and locks (any backend)

    Distributed mode is preferred when available, with same-box as fallback.

    Cache Invalidation Auto-Start:
        When caching is enabled and an event source is available, cache invalidation
        is automatically started on the first async operation (e.g., wait_for_changes).
        This ensures users don't need to manually call _start_cache_invalidation().
    """

    # Type hints for attributes from NexusFS parent class
    if TYPE_CHECKING:
        backend: Backend
        _file_watcher: FileWatcher | None
        _event_bus: EventBusBase | None
        _lock_manager: LockManagerBase | None
        _cache_invalidation_started: bool

        @property
        def zone_id(self) -> str | None: ...

        @property
        def agent_id(self) -> str | None: ...

        def _validate_path(self, path: str) -> str: ...

        def _get_routing_params(
            self, context: OperationContext | dict[Any, Any] | None
        ) -> tuple[str | None, str | None, bool]: ...

    def _is_same_box(self) -> bool:
        """Check if we're in same-box mode (local file watching available).

        Returns:
            True if using PassthroughBackend, False otherwise
        """
        return self.backend.is_passthrough

    def _has_distributed_events(self) -> bool:
        """Check if distributed event bus is available.

        Returns:
            True if GlobalEventBus is initialized, False otherwise
        """
        return hasattr(self, "_event_bus") and self._event_bus is not None

    def _has_distributed_locks(self) -> bool:
        """Check if distributed lock manager is available.

        Returns:
            True if DistributedLockManager is initialized, False otherwise
        """
        return hasattr(self, "_lock_manager") and self._lock_manager is not None

    def _get_file_watcher(self) -> FileWatcher:
        """Get or create the file watcher instance for same-box mode.

        Returns:
            FileWatcher instance

        Raises:
            NotImplementedError: If not in same-box mode
        """
        if not self._is_same_box():
            raise NotImplementedError(
                "File watching is only available with PassthroughBackend (same-box mode). "
                "For distributed scenarios, configure Redis for GlobalEventBus."
            )

        if not hasattr(self, "_file_watcher") or self._file_watcher is None:
            from nexus.core.file_watcher import FileWatcher

            self._file_watcher = FileWatcher()

        return self._file_watcher

    def _get_zone_id(self, context: OperationContext | None) -> str:
        """Get zone ID from context or default.

        Args:
            context: Operation context

        Returns:
            Zone ID string (defaults to "default")
        """
        if context and hasattr(context, "zone_id") and context.zone_id:
            return context.zone_id
        if hasattr(self, "zone_id") and self.zone_id:
            return self.zone_id
        return "default"

    def _should_auto__start_cache_invalidation(self) -> bool:
        """Check if cache invalidation should be auto-started.

        Returns True if:
        - Caching is enabled (metadata or content cache)
        - An event source is available (event bus or same-box)
        - Cache invalidation hasn't been started yet
        """
        # Check if already started
        if getattr(self, "_cache_invalidation_started", False):
            return False

        # Check if caching is enabled
        has_cache = False
        if hasattr(self, "metadata") and hasattr(self.metadata, "_cache"):
            has_cache = self.metadata._cache is not None

        if not has_cache:
            return False

        # Check if event source is available
        has_event_source = self._has_distributed_events() or self._is_same_box()

        return has_event_source

    async def _ensure_distributed_system_ready(self) -> None:
        """Ensure the distributed event system is ready for use.

        This method is called automatically on the first async operation that
        needs the event system. It:
        1. Starts the event bus if not already started (uses event bus's own _started flag as SSOT)
        2. Auto-starts cache invalidation if caching is enabled

        This lazy initialization ensures users don't need to manually call
        _start_cache_invalidation() - it happens automatically when needed.
        """
        # Start event bus if available and not started
        # SSOT: Use event bus's internal _started flag instead of maintaining our own
        if self._has_distributed_events() and not getattr(self._event_bus, "_started", False):
            try:
                await self._event_bus.start()  # type: ignore[union-attr]
                logger.debug("Event bus auto-started")
            except Exception as e:
                logger.warning(f"Failed to auto-start event bus: {e}")

        # Auto-start cache invalidation if conditions are met
        if self._should_auto__start_cache_invalidation():
            self._start_cache_invalidation()
            logger.info("🔄 Cache invalidation auto-started (caching enabled with event source)")

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
        - Layer 2 (preferred): Uses GlobalEventBus (Redis Pub/Sub) for distributed events
        - Layer 1 (fallback): Uses OS-native file watching (same-box only)

        Semantics:
        - File path (e.g., "/inbox/file.txt"): Watches for content changes only
        - Directory path (e.g., "/inbox/"): Watches for file create/delete/rename

        Args:
            path: Virtual path to watch
            timeout: Maximum time to wait in seconds (default: 30.0)
            since_revision: Only return events with revision > this value (Issue #1187).
                           Use this with zookie tokens for watch resumption:
                           ``zookie = Zookie.decode(token); since_revision=zookie.revision``
            _context: Operation context (optional)

        Returns:
            Dict with change info if change detected:
                - type: "file_write", "file_delete", "file_rename", etc.
                - path: Path that changed
                - old_path: Previous path (for rename events only)
                - revision: Event revision number (for zookie-based resumption)
            None if timeout reached

        Raises:
            NotImplementedError: If no event source is available

        Example:
            >>> # Watch for new files in inbox
            >>> change = await nexus.wait_for_changes("/inbox/", timeout=60)
            >>> if change:
            ...     print(f"Detected {change['type']} on {change['path']}")

            >>> # Resume watching from a zookie (Issue #1187)
            >>> zookie = Zookie.decode(write_result["zookie"])
            >>> change = await nexus.wait_for_changes("/inbox/", since_revision=zookie.revision)
        """
        # Auto-start distributed system and cache invalidation if needed
        await self._ensure_distributed_system_ready()

        path = self._validate_path(path)
        zone_id = self._get_zone_id(_context)

        # Layer 2: Distributed event bus (preferred)
        if self._has_distributed_events():
            logger.debug(f"Using distributed event bus for {path}")
            event = await self._event_bus.wait_for_event(  # type: ignore[union-attr]
                zone_id=zone_id,
                path_pattern=path,
                timeout=timeout,
                since_revision=since_revision,  # Issue #1187: Zookie-based resumption
            )
            if event is None:
                return None
            return event.to_dict()

        # Layer 1: Same-box local watching (fallback)
        if self._is_same_box():
            logger.debug(f"Using same-box file watcher for {path}")
            from nexus.backends.passthrough import PassthroughBackend

            # Type narrowing for PassthroughBackend-specific attributes below
            assert isinstance(self.backend, PassthroughBackend), "Backend mismatch"

            # Import FileEvent for unified response format
            from nexus.core.event_bus import FileEvent

            # Determine the physical watch path
            # For glob patterns, we need to watch the parent directory
            watch_path = path.rstrip("/")
            if "*" in path or "?" in path:
                # Glob pattern: extract base directory to watch
                # e.g., "/inbox/*.txt" -> watch "/inbox"
                import os

                watch_path = os.path.dirname(watch_path.split("*")[0].split("?")[0])
                if not watch_path:
                    watch_path = "/"

            physical_path = self.backend.get_physical_path(watch_path)
            watcher = self._get_file_watcher()

            # For glob patterns, we need to filter events
            is_pattern = "*" in path or "?" in path or path.endswith("/")
            deadline = None
            if is_pattern:
                deadline = asyncio.get_event_loop().time() + timeout

            while True:
                remaining_timeout = timeout
                if deadline is not None:
                    remaining_timeout = max(0, deadline - asyncio.get_event_loop().time())
                    if remaining_timeout <= 0:
                        return None

                change = await watcher.wait_for_change(physical_path, timeout=remaining_timeout)

                if change is None:
                    return None

                # Convert to unified FileEvent format
                event = FileEvent.from_file_change(change, zone_id=zone_id)

                # For glob/directory patterns, filter using matches_path_pattern
                if is_pattern:
                    # Reconstruct virtual path from physical change path
                    virtual_change_path = change.path
                    if not virtual_change_path.startswith("/"):
                        virtual_change_path = f"{watch_path.rstrip('/')}/{virtual_change_path}"

                    # Update event path to virtual path
                    event.path = virtual_change_path

                    if event.matches_path_pattern(path):
                        return event.to_dict()
                    # Pattern didn't match, continue waiting
                    logger.debug(f"Event {event.path} didn't match pattern {path}, continuing...")
                    continue

                return event.to_dict()

        # No event source available
        raise NotImplementedError(
            "No event source available. Either configure Redis for distributed events "
            "or use PassthroughBackend for same-box file watching."
        )

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

        Dual-track implementation:
        - Layer 2 (preferred): Uses DistributedLockManager (Redis) for distributed locks
        - Layer 1 (fallback): Uses in-memory locks (same-box only)

        Supports both mutex (max_holders=1) and semaphore (max_holders>1) modes.
        For long-running operations, use extend_lock() to keep the lock alive.

        Args:
            path: Virtual path to lock
            timeout: Maximum time to wait for lock in seconds (default: 30.0)
            ttl: Lock TTL in seconds - auto-expires after this (default: 30.0)
                 Only used in distributed mode.
            max_holders: Maximum concurrent holders (default: 1 = mutex)
                         Set >1 for semaphore mode (e.g., boardroom with N seats)
            _context: Operation context (optional)

        Returns:
            Lock ID if acquired (use this to unlock/extend later)
            None if timeout reached

        Raises:
            ValueError: If max_holders < 1 or max_holders mismatch (SSOT violation)

        Example (Mutex - exclusive lock):
            >>> lock_id = await nexus.lock("/shared/config.json", timeout=5.0)
            >>> if lock_id:
            ...     try:
            ...         # Perform exclusive operation
            ...         content = nexus.read("/shared/config.json")
            ...         nexus.write("/shared/config.json", modified_content)
            ...     finally:
            ...         await nexus.unlock(lock_id, "/shared/config.json")
            ... else:
            ...     print("Could not acquire lock")

        Example (Semaphore - boardroom with 5 seats):
            >>> lock_id = await nexus.lock("/rooms/board_01", max_holders=5)
            >>> if lock_id:
            ...     # One of up to 5 participants
            ...     await nexus.unlock(lock_id, "/rooms/board_01")

        Meeting Floor Control Example:
            >>> lock_id = await nexus.lock("/meeting/floor", timeout=5.0)
            >>> if lock_id:
            ...     # Start heartbeat in background
            ...     async def heartbeat():
            ...         while speaking:
            ...             await nexus.extend_lock(lock_id, "/meeting/floor")
            ...             await asyncio.sleep(15)
            ...     task = asyncio.create_task(heartbeat())
            ...     # Do speech...
            ...     task.cancel()
            ...     await nexus.unlock(lock_id, "/meeting/floor")
        """
        # Auto-start distributed system and cache invalidation if needed
        await self._ensure_distributed_system_ready()

        path = self._validate_path(path)
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
            from nexus.backends.passthrough import PassthroughBackend

            # Type narrowing for PassthroughBackend-specific attributes below
            assert isinstance(self.backend, PassthroughBackend), "Backend mismatch"

            # Note: Same-box locks don't support TTL - they're in-memory only
            lock_id = self.backend.lock(path, timeout=timeout, max_holders=max_holders)

            if lock_id:
                logger.debug(f"Same-box lock acquired on {path}: {lock_id}")
            else:
                logger.warning(f"Same-box lock timeout on {path} after {timeout}s")
            return lock_id

        # No lock manager available
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

        Use this to keep distributed locks alive during long operations.
        Call periodically (e.g., every TTL/2 seconds) to prevent lock expiry.

        For same-box locks (Layer 1), this is a no-op that returns True,
        since in-memory locks don't have TTL.

        Args:
            lock_id: Lock ID returned from lock()
            path: Path that was locked
            ttl: New TTL in seconds (default: 30.0)
            _context: Operation context (optional)

        Returns:
            True if lock was extended (or same-box mode)
            False if lock was not found or not owned

        Example:
            >>> lock_id = await nexus.lock("/meeting/floor")
            >>> # Heartbeat pattern for long operations
            >>> async def heartbeat():
            ...     while working:
            ...         success = await nexus.extend_lock(lock_id, "/meeting/floor")
            ...         if not success:
            ...             raise RuntimeError("Lost lock!")
            ...         await asyncio.sleep(15)  # Extend every 15s for 30s TTL
        """
        zone_id = self._get_zone_id(_context)

        # Layer 2: Distributed lock manager
        if self._has_distributed_locks():
            path = self._validate_path(path)
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

        # No lock manager available
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
            path: Path that was locked (required for distributed mode,
                  optional for same-box mode for backward compatibility)
            _context: Operation context (optional)

        Returns:
            True if lock was released
            False if lock_id was not found or not owned

        Example:
            >>> lock_id = await nexus.lock("/shared/config.json")
            >>> # ... do work ...
            >>> success = await nexus.unlock(lock_id, "/shared/config.json")
            >>> assert success
        """
        zone_id = self._get_zone_id(_context)

        # Layer 2: Distributed lock manager
        if self._has_distributed_locks():
            if path is None:
                raise ValueError("path is required for distributed unlock")
            path = self._validate_path(path)
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
            from nexus.backends.passthrough import PassthroughBackend

            # Type narrowing for PassthroughBackend-specific attributes below
            assert isinstance(self.backend, PassthroughBackend), "Backend mismatch"

            released = self.backend.unlock(lock_id)
            if released:
                logger.debug(f"Same-box lock released: {lock_id}")
            else:
                logger.warning(f"Same-box lock not found: {lock_id}")
            return released

        # No lock manager available
        raise NotImplementedError(
            "No lock manager available. Either configure Redis for distributed locks "
            "or use PassthroughBackend for same-box locking."
        )

    # =========================================================================
    # Lock Context Manager (Issue #1106 Block 3)
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

        This is the recommended API for read-modify-write operations where you
        need to hold a lock across multiple operations. The lock is automatically
        released when exiting the context, even if an exception occurs.

        For simple single-write locking, use `write(lock=True)` instead.
        For atomic read-modify-write, use `atomic_update()` instead.

        Args:
            path: Virtual path to lock
            timeout: Maximum time to wait for lock in seconds (default: 30.0)
            ttl: Lock TTL in seconds - auto-expires after this (default: 30.0)
            _context: Operation context (optional)

        Yields:
            lock_id: Lock identifier (can be used for extend_lock if needed)

        Raises:
            LockTimeout: If lock cannot be acquired within timeout
            NotImplementedError: If no lock manager is available

        Example:
            >>> # Read-modify-write with explicit locking
            >>> async with nx.locked("/config.json") as lock_id:
            ...     config = nx.read("/config.json")
            ...     config = json.loads(config)
            ...     config["version"] += 1
            ...     nx.write("/config.json", json.dumps(config), lock=False)

            >>> # Long-running operation with heartbeat
            >>> async with nx.locked("/meeting/floor", ttl=30.0) as lock_id:
            ...     async def heartbeat():
            ...         while speaking:
            ...             await nx.extend_lock(lock_id, "/meeting/floor")
            ...             await asyncio.sleep(15)
            ...     task = asyncio.create_task(heartbeat())
            ...     try:
            ...         await do_speech()
            ...     finally:
            ...         task.cancel()
        """
        from nexus.core.exceptions import LockTimeout

        # Acquire lock
        lock_id = await self.lock(path, timeout=timeout, ttl=ttl, _context=_context)

        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)

        try:
            yield lock_id
        finally:
            # Always release lock on exit
            await self.unlock(lock_id, path, _context=_context)

    # =========================================================================
    # Cache Invalidation via Events (Issue #1106 Block 2)
    # =========================================================================

    def _invalidate_cache_for_path(self, path: str) -> None:
        """Invalidate metadata cache for a path.

        Called by event callbacks when file changes are detected.

        Args:
            path: Path that changed (physical or virtual)
        """
        if hasattr(self, "metadata") and hasattr(self.metadata, "_cache"):
            cache = self.metadata._cache
            if cache is not None:
                # Convert physical path to virtual if needed
                virtual_path = path
                if self._is_same_box():
                    from nexus.backends.passthrough import PassthroughBackend

                    # Type narrowing: is_passthrough guarantees PassthroughBackend
                    assert isinstance(self.backend, PassthroughBackend)
                    # Strip base path to get virtual path
                    base_path = str(self.backend.base_path)
                    if path.startswith(base_path):
                        virtual_path = path[len(base_path) :]
                        if not virtual_path.startswith("/"):
                            virtual_path = "/" + virtual_path

                cache.invalidate_path(virtual_path)
                logger.debug(f"Cache invalidated: {virtual_path}")

    def _handle_cache_invalidation_event(
        self, event_type: Any, path: str, old_path: str | None
    ) -> None:
        """Handle cache invalidation for any event source (DRY helper).

        This is the single source of truth for which event types trigger
        cache invalidation and how they are handled.

        Args:
            event_type: FileEventType enum value
            path: Path that changed
            old_path: Previous path for rename events
        """
        from nexus.core.event_bus import FileEventType

        # Normalize event_type to enum if it's a string
        if isinstance(event_type, str):
            try:
                event_type = FileEventType(event_type)
            except ValueError:
                logger.debug(f"Unknown event type: {event_type}")
                return

        # SSOT: Event types that trigger cache invalidation
        # FILE_WRITE, FILE_DELETE, DIR_CREATE, DIR_DELETE invalidate single path
        # FILE_RENAME invalidates both old and new paths
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
        """Callback for Layer 1 (FileWatcher) file change events.

        Args:
            change: FileChange from file_watcher.py
        """
        from nexus.core.event_bus import FileEvent

        # Use FileEvent.from_file_change() as SSOT for type mapping
        # This avoids duplicating the ChangeType→FileEventType mapping
        event = FileEvent.from_file_change(change)
        self._handle_cache_invalidation_event(event.type, change.path, change.old_path)

    def _on_distributed_event(self, event: Any) -> None:
        """Callback for Layer 2 (EventBus) distributed events.

        Args:
            event: FileEvent from event_bus.py
        """
        self._handle_cache_invalidation_event(event.type, event.path, event.old_path)

    async def _async_handle_event(self, event: Any) -> None:
        """Async wrapper for handling events during startup sync.

        Args:
            event: FileEvent from event_bus.py
        """
        # Delegate to sync handler (cache invalidation is sync)
        self._on_distributed_event(event)

    def _start_cache_invalidation(self) -> None:
        """Start cache invalidation listeners for both Layer 1 and Layer 2.

        This method sets up event-driven cache invalidation:
        - Layer 1 (Same-box): Uses FileWatcher with OS-native callbacks
        - Layer 2 (Distributed): Uses EventBus Redis Pub/Sub subscription

        Cache invalidation ensures that when one NexusFS instance (or external
        process) modifies files, other instances see the updated content.

        Note:
            This method is called automatically when:
            - Caching is enabled (metadata or content cache)
            - An event source is available (Redis or same-box)
            - The first async operation is performed (e.g., wait_for_changes)

            You can also call it manually after NexusFS initialization.
            The method is idempotent - multiple calls are safe.
        """
        # Idempotent: return early if already started
        if getattr(self, "_cache_invalidation_started", False):
            logger.debug("Cache invalidation already started, skipping")
            return

        # Mark as started (set early to prevent race conditions)
        self._cache_invalidation_started = True

        # Layer 2: Distributed event bus (Redis Pub/Sub)
        if self._has_distributed_events():
            zone_id = self._get_zone_id(None)

            async def _subscribe_loop() -> None:
                """Background task to subscribe to distributed events."""
                try:
                    # Phase E: Startup Sync - reconcile missed events from PG SSOT
                    # This must happen BEFORE subscribing to new events
                    if hasattr(self._event_bus, "startup_sync"):
                        try:
                            synced = await self._event_bus.startup_sync(  # type: ignore[union-attr]  # allowed
                                event_handler=self._async_handle_event,
                            )
                            if synced > 0:
                                logger.info(f"Startup sync: processed {synced} missed events")
                        except Exception as e:
                            logger.warning(f"Startup sync failed (continuing anyway): {e}")

                    logger.info(f"Starting distributed cache invalidation for zone: {zone_id}")
                    assert self._event_bus is not None  # guaranteed by _has_distributed_events()
                    async for event in self._event_bus.subscribe(zone_id):
                        self._on_distributed_event(event)
                except asyncio.CancelledError:
                    logger.info("Distributed cache invalidation stopped")
                    raise
                except Exception as e:
                    logger.error(f"Distributed cache invalidation error: {e}")

            # Start as background task
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(_subscribe_loop())
                self._event_tasks.add(task)  # type: ignore[attr-defined]
                task.add_done_callback(self._event_tasks.discard)  # type: ignore[attr-defined]
            except RuntimeError:
                logger.debug("No running event loop, skipping distributed cache invalidation")

        # Layer 1: Same-box file watching (OS-native callbacks)
        if self._is_same_box():
            from nexus.backends.passthrough import PassthroughBackend

            # Type narrowing: is_passthrough guarantees PassthroughBackend
            assert isinstance(self.backend, PassthroughBackend)
            try:
                watcher = self._get_file_watcher()

                # Start watcher if not already started
                if not watcher._started:
                    try:
                        loop = asyncio.get_running_loop()
                        watcher.start(loop)
                    except RuntimeError:
                        watcher.start()

                # Watch the root directory
                root_path = self.backend.base_path
                watcher.add_watch(root_path, self._on_file_change, recursive=True)

                logger.info(f"Started same-box cache invalidation: {root_path}")
            except Exception as e:
                logger.warning(f"Could not start same-box cache invalidation: {e}")

    def _stop_cache_invalidation(self) -> None:
        """Stop all cache invalidation listeners.

        Cleans up both Layer 1 (FileWatcher) and Layer 2 (EventBus) listeners.
        After stopping, cache invalidation can be restarted by calling
        _start_cache_invalidation() again.
        """
        # Stop Layer 2 (distributed) tasks
        for task in list(self._event_tasks):  # type: ignore[attr-defined]
            if not task.done():
                task.cancel()

        # Stop Layer 1 (same-box) watcher
        if hasattr(self, "_file_watcher") and self._file_watcher is not None:
            self._file_watcher.stop()

        # Reset flag to allow restart
        self._cache_invalidation_started = False

        logger.debug("Cache invalidation stopped")
