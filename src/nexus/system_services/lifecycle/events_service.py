"""Events Service — file watching and advisory locking.

Dual-track event delivery for wait_for_changes():

- **Internal (OBSERVE)**: EventsService registers as a VFSObserver on
  KernelDispatch.  Local mutations trigger ``on_mutation()`` which resolves
  pending waiters via in-memory futures (~0µs).
- **EventBus (distributed)**: Remote mutations arrive via Dragonfly/NATS
  pub/sub through ``EventBusBase.wait_for_event()``.

When both paths are available, ``wait_for_changes()`` races them via
``asyncio.wait(FIRST_COMPLETED)`` — local writes resolve instantly via
the internal observer; remote writes arrive via EventBus.

Known limitation: Raft apply on followers writes to redb directly via Rust
and does NOT call dispatch.notify().  The EventBus path covers this gap.
A future task should add a PyO3 callback from Rust apply → Python
dispatch.notify() for full internal-only coverage.

Phase 2: Core Refactoring (Issue #1287)
Extracted from: nexus_fs_events.py (836 lines)
"""

import asyncio
import contextlib
import dataclasses
import logging
import threading
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.core.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.file_events import FileEvent
    from nexus.lib.distributed_lock import LockManagerBase
    from nexus.system_services.event_bus.base import EventBusBase


@dataclasses.dataclass
class _Waiter:
    """Internal waiter for OBSERVE-path event delivery."""

    path_pattern: str
    future: asyncio.Future["FileEvent"]
    loop: asyncio.AbstractEventLoop


class EventsService:
    """Events service — file watching via kernel OBSERVE + EventBus.

    Implements VFSObserver (``on_mutation``) so that KernelDispatch.notify()
    delivers FileEvents directly.  Also consumes EventBus for remote writes.

    Architecture:
        - Registers as VFSObserver on KernelDispatch (OBSERVE phase)
        - on_mutation() resolves pending waiters in-memory (~0µs)
        - EventBus covers remote writes (Raft followers)
        - Races both when available (FIRST_COMPLETED)
    """

    def __init__(
        self,
        event_bus: "EventBusBase | None" = None,
        lock_manager: "LockManagerBase | None" = None,
        zone_id: str | None = None,
    ):
        self._event_bus = event_bus
        self._lock_manager = lock_manager
        self._zone_id = zone_id
        self._event_tasks: set[asyncio.Task[Any]] = set()

        # OBSERVE-path waiter state (thread-safe: dispatch.notify is sync)
        self._waiters: list[_Waiter] = []
        self._waiters_lock = threading.Lock()
        # Set to True after factory registers us as VFSObserver
        self._observe_registered = False

        logger.info("[EventsService] Initialized")

    # =========================================================================
    # HotSwappable protocol (Q2 — Issue #1611)
    # =========================================================================

    def hook_spec(self) -> HookSpec:
        """Declare VFS hooks: EventsService registers itself as an OBSERVE observer."""
        return HookSpec(observers=(self,))

    async def drain(self) -> None:
        """Stop accepting new waiter registrations."""
        self._observe_registered = False

    async def activate(self) -> None:
        """Resume accepting waiter registrations."""
        self._observe_registered = True

    # =========================================================================
    # Infrastructure Detection
    # =========================================================================

    def _has_internal_observe(self) -> bool:
        """Check if kernel OBSERVE path is active (registered as VFSObserver)."""
        return self._observe_registered

    def _has_distributed_events(self) -> bool:
        """Check if distributed event bus is available."""
        return self._event_bus is not None

    def _has_distributed_locks(self) -> bool:
        """Check if distributed lock manager is available."""
        return self._lock_manager is not None

    def _get_zone_id(self, context: "OperationContext | None") -> str:
        """Get zone ID from context or default."""
        if context and hasattr(context, "zone_id") and context.zone_id:
            return context.zone_id
        if self._zone_id:
            return self._zone_id
        return ROOT_ZONE_ID

    # =========================================================================
    # VFSObserver implementation (OBSERVE phase)
    # =========================================================================

    def on_mutation(self, event: "FileEvent") -> None:
        """Called by KernelDispatch.notify() on every local mutation.

        Matches the event against pending waiters and resolves their futures.
        Thread-safe: dispatch.notify() may be called from non-event-loop threads.
        """
        with self._waiters_lock:
            for w in self._waiters:
                if not w.future.done() and event.matches_path_pattern(w.path_pattern):
                    w.loop.call_soon_threadsafe(w.future.set_result, event)

    # =========================================================================
    # Internal wait (OBSERVE path)
    # =========================================================================

    async def _wait_internal(
        self,
        path: str,
        timeout: float,
    ) -> "FileEvent | None":
        """Wait for a local mutation via OBSERVE-path future."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future["FileEvent"] = loop.create_future()
        waiter = _Waiter(path_pattern=path, future=future, loop=loop)

        with self._waiters_lock:
            self._waiters.append(waiter)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None
        finally:
            with self._waiters_lock, contextlib.suppress(ValueError):
                self._waiters.remove(waiter)

    # =========================================================================
    # EventBus wait (distributed path)
    # =========================================================================

    async def _wait_eventbus(
        self,
        zone_id: str,
        path: str,
        timeout: float,
    ) -> "FileEvent | None":
        """Wait for a remote mutation via EventBus subscription."""
        event = await self._event_bus.wait_for_event(  # type: ignore[union-attr]
            zone_id=zone_id,
            path_pattern=path,
            timeout=timeout,
        )
        return event

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
    # System Readiness
    # =========================================================================

    async def _ensure_distributed_system_ready(self) -> None:
        """Ensure the distributed event system is ready for use."""
        if self._has_distributed_events() and not getattr(self._event_bus, "_started", False):
            try:
                await self._event_bus.start()  # type: ignore[union-attr]
                logger.debug("Event bus auto-started")
            except Exception as e:
                logger.warning(f"Failed to auto-start event bus: {e}")

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

        Uses kernel OBSERVE for local writes (~0µs) and EventBus for remote
        writes.  When both are available, races them via FIRST_COMPLETED.

        Args:
            path: Virtual path to watch (supports glob patterns)
            timeout: Maximum time to wait in seconds (default: 30.0)
            _context: Operation context (optional)

        Returns:
            Dict with change info if change detected, None if timeout
        """
        await self._ensure_distributed_system_ready()

        path = validate_path(path, allow_root=True)
        zone_id = self._get_zone_id(_context)

        has_internal = self._has_internal_observe()
        has_eventbus = self._has_distributed_events()

        if has_internal and has_eventbus:
            # Race both: local OBSERVE vs EventBus — first to fire wins
            task_internal = asyncio.create_task(self._wait_internal(path, timeout))
            task_eventbus = asyncio.create_task(self._wait_eventbus(zone_id, path, timeout))

            done, pending = await asyncio.wait(
                {task_internal, task_eventbus},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

            result_event = done.pop().result()
            if result_event is None:
                return None
            return result_event.to_dict()

        if has_internal:
            event = await self._wait_internal(path, timeout)
            if event is None:
                return None
            return event.to_dict()

        if has_eventbus:
            logger.debug(f"Using distributed event bus for {path}")
            event = await self._wait_eventbus(zone_id, path, timeout)
            if event is None:
                return None
            return event.to_dict()

        raise NotImplementedError(
            "No event source available. Either register EventsService as "
            "VFSObserver on KernelDispatch or configure EventBus for "
            "distributed events."
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

        if self._has_distributed_locks():
            mode = "mutex" if max_holders == 1 else f"semaphore({max_holders})"
            logger.debug(f"Using distributed lock manager for {path} ({mode})")
            lock_id = await self._lock_manager.acquire(  # type: ignore[union-attr]
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

        raise NotImplementedError(
            "No lock manager available. Configure a metastore that implements "
            "LockStoreProtocol (factory creates LocalLockManager or RaftLockManager)."
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
        if self._has_distributed_locks():
            path = validate_path(path, allow_root=True)
            extended = await self._lock_manager.extend(  # type: ignore[union-attr]
                lock_id=lock_id,
                path=path,
                ttl=ttl,
            )
            if extended.success:
                logger.debug(f"Lock extended: {lock_id} (TTL: {ttl}s)")
            else:
                logger.warning(f"Lock extend failed (not owned or expired): {lock_id}")
            return extended.success

        raise NotImplementedError(
            "No lock manager available. Configure a metastore that implements "
            "LockStoreProtocol (factory creates LocalLockManager or RaftLockManager)."
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
        if self._has_distributed_locks():
            if path is None:
                raise ValueError("path is required for distributed unlock")
            path = validate_path(path, allow_root=True)
            released = await self._lock_manager.release(  # type: ignore[union-attr]
                lock_id=lock_id,
                path=path,
            )
            if released:
                logger.debug(f"Distributed lock released: {lock_id}")
            else:
                logger.warning(f"Distributed lock not found: {lock_id}")
            return released

        raise NotImplementedError(
            "No lock manager available. Configure a metastore that implements "
            "LockStoreProtocol (factory creates LocalLockManager or RaftLockManager)."
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
