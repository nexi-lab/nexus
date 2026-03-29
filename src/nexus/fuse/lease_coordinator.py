"""Lease-aware FUSE cache coordinator — service-level lease integration.

Replaces direct ``FUSECacheManager`` access on ``FUSESharedContext`` with a
coordinator that adds lease-based cache coherence across mounts.

Design decisions (Issue #3397 review):
    - Decision 1A: Dedicated event loop thread for async lease operations
    - Decision 2A: holder_id = mount_id for cross-mount coherence
    - Decision 4A: Augment — direct local invalidation + fire-and-forget
                   lease revocation for cross-mount
    - Decision 5A: Generic ``lease_gated_get()`` eliminates 3× repetition
    - Decision 6A: Consolidated ``invalidate_and_revoke()``
    - Decision 7A: Path-based resource_id (``fuse:{path}``)
    - Decision 8A: Coordinator replaces ``ctx.cache`` on FUSESharedContext
    - Decision 11A: Fetch without caching on lease timeout
    - Decision 13A: Local validity cache avoids thread-switch on hot path
    - Decision 15A: Fire-and-forget revocation (writer never blocks)
    - Decision 16A: One lease per file path

Architecture:
    FUSE ops (sync, OS threads)
        → coordinator.lease_gated_get()
            → local validity cache hit? (~100ns) → serve from FUSECacheManager
            → miss → async lease validate/acquire via event loop thread
        → coordinator.invalidate_and_revoke()
            → immediate local cache invalidation
            → fire-and-forget lease revocation → callbacks on other mounts

References:
    - DFUSE paper: https://arxiv.org/abs/2503.18191
    - Gray & Cheriton: Leases for distributed cache consistency
    - Issue #3397: FUSE mount — service-level lease integration
    - Issue #3407: Common LeaseManager utility (blocked-by)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from nexus.contracts.protocols.lease import Lease, LeaseManagerProtocol, LeaseState
from nexus.fuse.cache import FUSECacheManager

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Resource key prefix for FUSE leases
_FUSE_RESOURCE_PREFIX = "fuse:"

# Default lease TTL for FUSE cache entries
_DEFAULT_LEASE_TTL = 30.0

# Default timeout for lease acquisition (non-blocking for hot path)
_LEASE_ACQUIRE_TIMEOUT = 5.0


class FUSELeaseCoordinator:
    """Lease-aware cache coordinator for FUSE operations.

    Wraps ``FUSECacheManager`` with lease-based cache coherence. Each mount
    gets a unique ``holder_id``; a shared ``LeaseManager`` across mounts
    provides cross-mount invalidation via revocation callbacks.

    Hot-path optimization (Decision 13A):
        A local validity cache (``{path: expires_at}``) avoids the async
        thread switch for every cached read. Only expired or missing entries
        trigger the full async lease validation path (~50-200μs). Valid
        entries are served in ~100ns (plain dict lookup + monotonic compare).

    Example::

        coordinator = FUSELeaseCoordinator(
            cache=FUSECacheManager(...),
            lease_manager=lease_mgr,
            holder_id="mount-abc123",
        )

        # Lease-gated attr read
        attrs = coordinator.lease_gated_get(
            path="/file.txt",
            cache_get=lambda: coordinator.get_attr("/file.txt"),
            cache_set=lambda v: coordinator.cache_attr("/file.txt", v),
            fetch_fn=lambda: backend_getattr("/file.txt"),
        )

        # Mutation with cross-mount invalidation
        coordinator.invalidate_and_revoke(["/file.txt"])
    """

    def __init__(
        self,
        cache: FUSECacheManager,
        lease_manager: LeaseManagerProtocol | None = None,
        holder_id: str = "default-mount",
        lease_ttl: float = _DEFAULT_LEASE_TTL,
        acquire_timeout: float = _LEASE_ACQUIRE_TIMEOUT,
    ) -> None:
        self._cache = cache
        self._lease_manager: LeaseManagerProtocol | None = lease_manager
        self._holder_id = holder_id
        self._lease_ttl = lease_ttl
        self._acquire_timeout = acquire_timeout

        # Local validity cache (Decision 13A)
        # {path: expires_at_monotonic} — avoids async thread switch on hot path
        self._validity: dict[str, float] = {}
        self._validity_lock = threading.Lock()

        # Dedicated event loop thread for async lease operations (Decision 1A)
        self._lease_loop: asyncio.AbstractEventLoop | None = None
        self._lease_thread: threading.Thread | None = None
        self._closed = False

        if self._lease_manager is not None:
            self._start_lease_loop()
            self._register_revocation_callback()

    # ------------------------------------------------------------------
    # Event loop thread (Decision 1A)
    # ------------------------------------------------------------------

    def _start_lease_loop(self) -> None:
        """Start dedicated event loop thread for lease operations."""
        ready = threading.Event()

        def _run_loop() -> None:
            self._lease_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._lease_loop)
            ready.set()
            self._lease_loop.run_forever()

        self._lease_thread = threading.Thread(target=_run_loop, daemon=True, name="fuse-lease-loop")
        self._lease_thread.start()
        ready.wait(timeout=2.0)

    def _register_revocation_callback(self) -> None:
        """Register callback so lease revocations clear our validity cache + L1 cache."""
        assert self._lease_manager is not None
        callback_id = f"fuse-coordinator-{self._holder_id}"

        async def _on_revocation(lease: Lease, reason: str) -> None:
            # Only invalidate if the revoked lease belongs to US — it means
            # another mount's action caused our lease to be revoked, so our
            # local cache is now stale and must be cleared.
            # (When we revoke others' leases, they handle their own cleanup.)
            if lease.holder_id != self._holder_id:
                return
            path = lease.resource_id
            if path.startswith(_FUSE_RESOURCE_PREFIX):
                path = path[len(_FUSE_RESOURCE_PREFIX) :]
            self._clear_validity(path)
            self._cache.invalidate_path(path)
            logger.debug(
                "[FUSE-LEASE] Revocation callback: path=%s reason=%s holder=%s",
                path,
                reason,
                lease.holder_id,
            )

        self._lease_manager.register_revocation_callback(callback_id, _on_revocation)

    def _submit_async(self, coro: Coroutine[Any, Any, T]) -> T:
        """Submit an async coroutine to the lease event loop and block for result.

        Used for lease operations that need to cross the sync/async boundary.
        """
        if self._lease_loop is None or self._lease_loop.is_closed():
            raise RuntimeError("Lease event loop not running")
        future: concurrent.futures.Future[T] = asyncio.run_coroutine_threadsafe(
            coro, self._lease_loop
        )
        return future.result(timeout=self._acquire_timeout + 1.0)

    def _fire_and_forget(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Submit an async coroutine without waiting for result (Decision 15A)."""
        if self._lease_loop is None or self._lease_loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(coro, self._lease_loop)

    # ------------------------------------------------------------------
    # Local validity cache (Decision 13A)
    # ------------------------------------------------------------------

    def _check_validity(self, path: str) -> bool:
        """Check if path has a valid lease in local validity cache. ~100ns."""
        with self._validity_lock:
            expires_at = self._validity.get(path)
        if expires_at is None:
            return False
        return time.monotonic() < expires_at

    def _set_validity(self, path: str, expires_at: float) -> None:
        """Record lease validity for a path."""
        with self._validity_lock:
            self._validity[path] = expires_at

    def _clear_validity(self, path: str) -> None:
        """Clear validity for a path (on local invalidation or revocation callback)."""
        with self._validity_lock:
            self._validity.pop(path, None)

    def _clear_all_validity(self) -> None:
        """Clear all validity entries."""
        with self._validity_lock:
            self._validity.clear()

    # ------------------------------------------------------------------
    # Lease operations (async, submitted to lease event loop)
    # ------------------------------------------------------------------

    def _acquire_read_lease(self, path: str) -> Lease | None:
        """Acquire a SHARED_READ lease for a path. Blocks until granted or timeout."""
        if self._lease_manager is None:
            return None
        resource_id = f"{_FUSE_RESOURCE_PREFIX}{path}"
        try:
            lease: Lease | None = self._submit_async(
                self._lease_manager.acquire(
                    resource_id,
                    self._holder_id,
                    LeaseState.SHARED_READ,
                    ttl=self._lease_ttl,
                    timeout=self._acquire_timeout,
                )
            )
            if lease is not None:
                self._set_validity(path, lease.expires_at)
            return lease
        except Exception:
            logger.debug("[FUSE-LEASE] Lease acquire failed for %s", path, exc_info=True)
            return None

    def _validate_lease(self, path: str) -> Lease | None:
        """Validate that we still hold a lease for a path."""
        if self._lease_manager is None:
            return None
        resource_id = f"{_FUSE_RESOURCE_PREFIX}{path}"
        try:
            result: Lease | None = self._submit_async(
                self._lease_manager.validate(resource_id, self._holder_id)
            )
            return result
        except Exception:
            logger.debug("[FUSE-LEASE] Lease validate failed for %s", path, exc_info=True)
            return None

    def _revoke_lease_async(self, path: str) -> None:
        """Fire-and-forget lease revocation (Decision 15A)."""
        if self._lease_manager is None:
            return
        resource_id = f"{_FUSE_RESOURCE_PREFIX}{path}"
        self._fire_and_forget(self._lease_manager.revoke(resource_id))

    # ------------------------------------------------------------------
    # Core API: lease_gated_get (Decision 5A)
    # ------------------------------------------------------------------

    def lease_gated_get(
        self,
        path: str,
        cache_get: Callable[[], T | None],
        cache_set: Callable[[T], None],
        fetch_fn: Callable[[], T],
    ) -> T:
        """Lease-gated cache read with automatic lease management.

        Flow:
            1. Check local validity cache (~100ns)
            2. If valid → check L1 cache → return on hit
            3. If expired/miss → full lease validate/acquire
            4. Fetch from backend → cache under lease
            5. On lease timeout → fetch without caching (Decision 11A)

        Args:
            path: File path (used as lease resource_id)
            cache_get: Callable that returns cached value or None
            cache_set: Callable that stores a value in cache
            fetch_fn: Callable that fetches the value from backend

        Returns:
            The cached or freshly-fetched value
        """
        # No lease manager → behave like plain cache (backward compatible)
        if self._lease_manager is None:
            cached = cache_get()
            if cached is not None:
                return cached
            result = fetch_fn()
            cache_set(result)
            return result

        # Step 1: Hot path — local validity cache check (~100ns)
        if self._check_validity(path):
            cached = cache_get()
            if cached is not None:
                return cached
            # Valid lease but cache miss — fetch and cache
            result = fetch_fn()
            cache_set(result)
            return result

        # Step 2: Validity expired or missing — full lease validation
        lease = self._validate_lease(path)
        if lease is not None:
            # Lease still valid — update local validity cache and check L1
            self._set_validity(path, lease.expires_at)
            cached = cache_get()
            if cached is not None:
                return cached
            result = fetch_fn()
            cache_set(result)
            return result

        # Step 3: No valid lease — acquire new one
        lease = self._acquire_read_lease(path)
        if lease is not None:
            # Acquired — fetch and cache under lease
            result = fetch_fn()
            cache_set(result)
            return result

        # Step 4: Lease acquisition timed out (Decision 11A)
        # Fetch from backend without caching — availability >= baseline
        logger.warning("[FUSE-LEASE] Lease timeout for %s — serving without cache", path)
        return fetch_fn()

    # ------------------------------------------------------------------
    # Core API: invalidate_and_revoke (Decision 6A)
    # ------------------------------------------------------------------

    def invalidate_and_revoke(self, paths: list[str]) -> None:
        """Invalidate local caches and fire-and-forget lease revocation.

        Local invalidation is immediate (Decision 4A). Cross-mount revocation
        is asynchronous (Decision 15A).

        Args:
            paths: List of file paths to invalidate
        """
        for path in paths:
            # Immediate local invalidation
            self._cache.invalidate_path(path)
            self._clear_validity(path)
            # Fire-and-forget cross-mount revocation
            self._revoke_lease_async(path)

    # ------------------------------------------------------------------
    # Delegated cache methods (backward compatibility)
    # ------------------------------------------------------------------

    def get_attr(self, path: str) -> dict[str, Any] | None:
        """Get cached file attributes (direct, no lease check)."""
        return self._cache.get_attr(path)

    def cache_attr(self, path: str, attrs: dict[str, Any]) -> None:
        """Cache file attributes."""
        self._cache.cache_attr(path, attrs)

    def get_content(self, path: str) -> bytes | None:
        """Get cached file content (direct, no lease check)."""
        return self._cache.get_content(path)

    def cache_content(self, path: str, content: bytes) -> None:
        """Cache file content."""
        self._cache.cache_content(path, content)

    def get_parsed(self, path: str, view_type: str) -> bytes | None:
        """Get cached parsed content (direct, no lease check)."""
        return self._cache.get_parsed(path, view_type)

    def get_parsed_size(self, path: str, view_type: str) -> int | None:
        """Get size of cached parsed content."""
        return self._cache.get_parsed_size(path, view_type)

    def cache_parsed(self, path: str, view_type: str, content: bytes) -> None:
        """Cache parsed content."""
        self._cache.cache_parsed(path, view_type, content)

    def invalidate_path(self, path: str) -> None:
        """Invalidate all caches for a path (local only, no lease revocation)."""
        self._cache.invalidate_path(path)
        self._clear_validity(path)

    def invalidate_all(self) -> None:
        """Invalidate all caches."""
        self._cache.invalidate_all()
        self._clear_all_validity()

    def get_metrics(self) -> dict[str, Any]:
        """Get cache metrics."""
        return self._cache.get_metrics()

    def reset_metrics(self) -> None:
        """Reset cache metrics."""
        self._cache.reset_metrics()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def holder_id(self) -> str:
        """The mount's holder ID for lease operations."""
        return self._holder_id

    @property
    def lease_manager(self) -> LeaseManagerProtocol | None:
        """The shared lease manager (for diagnostics)."""
        return self._lease_manager

    def close(self) -> None:
        """Shut down lease state and event loop thread. Idempotent.

        Cleanup steps:
        1. Unregister our revocation callback (prevents stale callback invocations)
        2. Revoke all leases held by this mount (frees resources for other mounts)
        3. Stop the event loop thread
        """
        if self._closed:
            return
        self._closed = True

        # Unregister callback + revoke holder's leases before stopping the loop
        if self._lease_manager is not None:
            callback_id = f"fuse-coordinator-{self._holder_id}"
            self._lease_manager.unregister_revocation_callback(callback_id)
            # Revoke all leases held by this mount (best-effort)
            if self._lease_loop is not None and not self._lease_loop.is_closed():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._lease_manager.revoke_holder(self._holder_id),
                        self._lease_loop,
                    )
                    future.result(timeout=2.0)
                except Exception:
                    logger.debug(
                        "[FUSE-LEASE] Best-effort holder revocation failed",
                        exc_info=True,
                    )

        if self._lease_loop is not None and not self._lease_loop.is_closed():
            self._lease_loop.call_soon_threadsafe(self._lease_loop.stop)
        if self._lease_thread is not None:
            self._lease_thread.join(timeout=2.0)
        logger.debug("[FUSE-LEASE] Coordinator closed (holder=%s)", self._holder_id)
