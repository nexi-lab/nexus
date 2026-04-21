"""Local in-memory lease manager — shared foundation for DFUSE-inspired optimizations.

Implements ``LeaseManagerProtocol`` with:
- DFUSE-style conflict resolution (revoke conflicting holders before granting)
- Dual index (by-resource + by-holder) for O(1) lookups
- Monotonically increasing fencing tokens (``generation``) per resource
- Injectable ``Clock`` protocol for deterministic testing
- Revocation callback registry with concurrent execution + per-callback timeout
- Lazy expiry on access + periodic background sweep for abandoned leases
- Single ``asyncio.Lock`` — sufficient for in-memory state (no I/O under lock)
- Blocking acquire with exponential backoff

Architecture:
    - ``LocalLeaseManager``: Single-process mode (this file)
    - ``LeaseManagerProtocol``: ``contracts/protocols/lease.py``
    - Service owner: ``services/lifecycle/lease_service.py``

References:
    - DFUSE paper: https://arxiv.org/abs/2503.18191
    - Issue #3407: Common LeaseManager utility
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.lease import (
    Clock,
    Lease,
    LeaseState,
    RevocationCallback,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Clock implementations
# =============================================================================


class SystemClock:
    """Production clock backed by ``time.monotonic()``."""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class ManualClock:
    """Test clock with explicit time advancement.

    Example::

        clock = ManualClock(now=0.0)
        clock.advance(10.0)  # now == 10.0
    """

    _now: float = 0.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """Advance the clock by the given number of seconds."""
        if seconds < 0:
            raise ValueError(f"Cannot advance clock by negative seconds: {seconds}")
        self._now += seconds

    def set(self, now: float) -> None:
        """Set the clock to an absolute monotonic time."""
        self._now = now


# =============================================================================
# Compatibility matrix — DFUSE S3.1
# =============================================================================

# (existing_state, requested_state) -> compatible?
_COMPATIBLE: dict[tuple[LeaseState, LeaseState], bool] = {
    (LeaseState.SHARED_READ, LeaseState.SHARED_READ): True,
    (LeaseState.SHARED_READ, LeaseState.EXCLUSIVE_WRITE): False,
    (LeaseState.EXCLUSIVE_WRITE, LeaseState.SHARED_READ): False,
    (LeaseState.EXCLUSIVE_WRITE, LeaseState.EXCLUSIVE_WRITE): False,
}


def _is_compatible(existing: LeaseState, requested: LeaseState) -> bool:
    """Check if an existing lease state is compatible with a requested state."""
    return _COMPATIBLE.get((existing, requested), False)


# =============================================================================
# Local in-memory lease manager
# =============================================================================

# Retry / backoff constants
_BASE_RETRY_INTERVAL = 0.05  # 50ms base retry interval
_MAX_RETRY_INTERVAL = 1.0  # 1s maximum retry interval
_BACKOFF_MULTIPLIER = 2.0  # exponential backoff multiplier

# Callback timeout
_CALLBACK_TIMEOUT_S = 5.0  # per-callback timeout for revocation callbacks

# Background sweep interval
_SWEEP_INTERVAL_S = 60.0  # how often to sweep expired leases


class LocalLeaseManager:
    """In-memory lease manager for single-process use.

    Provides DFUSE-style shared-read / exclusive-write leases with
    conflict resolution, fencing tokens, and revocation callbacks.

    zone_id is bound at construction — callers never pass it per-method.
    Internally used as a key prefix for resource scoping.

    Example::

        mgr = LocalLeaseManager(zone_id="us-east-1")
        lease = await mgr.acquire("file:123", "agent-A", LeaseState.SHARED_READ)
        if lease:
            valid = await mgr.validate("file:123", "agent-A")
            await mgr.revoke("file:123", holder_id="agent-A")
        await mgr.close()
    """

    DEFAULT_TTL = 30.0  # Default lease TTL in seconds
    DEFAULT_TIMEOUT = 30.0  # Default acquisition timeout in seconds

    def __init__(
        self,
        *,
        zone_id: str = ROOT_ZONE_ID,
        clock: Clock | None = None,
        sweep_interval: float = _SWEEP_INTERVAL_S,
        callback_timeout: float = _CALLBACK_TIMEOUT_S,
    ) -> None:
        self._zone_id = zone_id
        self._clock: Clock = clock or SystemClock()
        self._sweep_interval = sweep_interval
        self._callback_timeout = callback_timeout

        # Dual index for efficient lookups (Decision #13A)
        # by_resource[resource_key][holder_id] -> Lease
        self._by_resource: dict[str, dict[str, Lease]] = {}
        # by_holder[holder_id] -> set of resource_keys
        self._by_holder: dict[str, set[str]] = {}

        # Fencing token: monotonically increasing per resource
        self._generation: dict[str, int] = {}

        # Single async lock (Decision #15A — no I/O under lock)
        self._lock = asyncio.Lock()

        # Revocation callback registry (Decision #3A — CacheCoordinator pattern)
        self._callbacks: list[tuple[str, RevocationCallback]] = []

        # Stats counters
        self._acquire_count = 0
        self._revoke_count = 0
        self._extend_count = 0
        self._timeout_count = 0
        self._callback_error_count = 0

        # Background sweep task (Decision #14A)
        self._sweep_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- resource key helpers -------------------------------------------------

    def _resource_key(self, resource_id: str) -> str:
        """Compose store-level key from zone_id + resource_id."""
        return f"{self._zone_id}:{resource_id}"

    # -- background sweep (Decision #14A) -------------------------------------

    def _ensure_sweep_task(self) -> None:
        """Start the background sweep task if not already running."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop(), name="lease-sweep")

    async def _sweep_loop(self) -> None:
        """Periodically sweep expired leases to prevent unbounded memory growth."""
        while not self._closed:
            try:
                await asyncio.sleep(self._sweep_interval)
            except asyncio.CancelledError:
                return
            await self._sweep_expired()

    async def _sweep_expired(self) -> None:
        """Remove all expired leases from internal state and notify callbacks."""
        now = self._clock.monotonic()
        expired_leases: list[Lease] = []
        async with self._lock:
            for rkey in list(self._by_resource.keys()):
                expired_leases.extend(self._evict_expired_for_resource(rkey, now))
            if expired_leases:
                logger.debug(
                    "[LeaseManager] Sweep removed %d expired lease(s)",
                    len(expired_leases),
                )
        # Invoke callbacks outside lock
        if expired_leases:
            await self._invoke_callbacks(expired_leases, "expired")

    # -- internal state management (call under self._lock) --------------------

    def _evict_expired_for_resource(self, rkey: str, now: float) -> list[Lease]:
        """Lazily evict expired leases for a single resource (under lock).

        Returns:
            List of expired leases that were removed (for callback invocation).
        """
        holders = self._by_resource.get(rkey)
        if not holders:
            return []
        expired_leases: list[Lease] = []
        expired_hids = [hid for hid, lease in holders.items() if lease.is_expired(now)]
        for hid in expired_hids:
            removed = self._remove_lease_unlocked(rkey, hid)
            if removed is not None:
                expired_leases.append(removed)
        return expired_leases

    def _remove_lease_unlocked(self, rkey: str, holder_id: str) -> Lease | None:
        """Remove a lease from both indexes (under lock). Returns removed lease."""
        holders = self._by_resource.get(rkey)
        if not holders:
            return None
        lease = holders.pop(holder_id, None)
        if lease is None:
            return None
        # Clean up empty resource entry
        if not holders:
            del self._by_resource[rkey]
        # Update holder index
        holder_resources = self._by_holder.get(holder_id)
        if holder_resources is not None:
            holder_resources.discard(rkey)
            if not holder_resources:
                del self._by_holder[holder_id]
        return lease

    def _store_lease_unlocked(self, rkey: str, lease: Lease) -> None:
        """Store a lease in both indexes (under lock)."""
        self._by_resource.setdefault(rkey, {})[lease.holder_id] = lease
        self._by_holder.setdefault(lease.holder_id, set()).add(rkey)

    def _next_generation(self, rkey: str) -> int:
        """Get and increment the fencing token for a resource (under lock)."""
        gen = self._generation.get(rkey, 0) + 1
        self._generation[rkey] = gen
        return gen

    def _get_conflicts_unlocked(
        self,
        rkey: str,
        holder_id: str,
        requested: LeaseState,
        now: float,
    ) -> list[Lease]:
        """Return list of active leases that conflict with the requested state."""
        holders = self._by_resource.get(rkey)
        if not holders:
            return []
        conflicts: list[Lease] = []
        for hid, lease in holders.items():
            if hid == holder_id:
                continue  # same holder — not a conflict
            if lease.is_expired(now):
                continue
            if not _is_compatible(lease.state, requested):
                conflicts.append(lease)
        return conflicts

    # -- revocation callbacks (Decision #3A + #16A+C) -------------------------

    async def _invoke_callbacks(self, leases: list[Lease], reason: str) -> None:
        """Invoke all registered callbacks concurrently with per-callback timeout.

        Failing callbacks are logged and do not prevent revocation.
        """
        if not self._callbacks or not leases:
            return

        async def _safe_invoke(cb_id: str, cb: RevocationCallback, lease: Lease) -> None:
            try:
                await asyncio.wait_for(cb(lease, reason), timeout=self._callback_timeout)
            except TimeoutError:
                logger.warning(
                    "[LeaseManager] Callback %s timed out for %s:%s",
                    cb_id,
                    lease.resource_id,
                    lease.holder_id,
                )
                self._callback_error_count += 1
            except Exception:
                logger.warning(
                    "[LeaseManager] Callback %s failed for %s:%s",
                    cb_id,
                    lease.resource_id,
                    lease.holder_id,
                    exc_info=True,
                )
                self._callback_error_count += 1

        tasks = [
            _safe_invoke(cb_id, cb, lease) for cb_id, cb in self._callbacks for lease in leases
        ]
        await asyncio.gather(*tasks)

    # -- public API -----------------------------------------------------------

    async def acquire(
        self,
        resource_id: str,
        holder_id: str,
        state: LeaseState,
        *,
        ttl: float = DEFAULT_TTL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Lease | None:
        """Acquire a lease, blocking until conflicts are resolved or timeout.

        Follows DFUSE Algorithm 2 (GrantLease): conflicting holders are
        revoked (with callbacks) before the new lease is granted.

        If the same holder already holds a compatible lease on the same
        resource, the existing lease is returned (idempotent).  If the
        same holder holds an incompatible lease (upgrade), the old lease
        is released first.
        """
        self._ensure_sweep_task()
        rkey = self._resource_key(resource_id)
        deadline = self._clock.monotonic() + timeout
        retry_interval = _BASE_RETRY_INTERVAL

        while True:
            now = self._clock.monotonic()
            if timeout > 0 and now >= deadline:
                self._timeout_count += 1
                logger.debug(
                    "[LeaseManager] Acquire timeout: %s by %s",
                    resource_id,
                    holder_id,
                )
                return None

            expired_in_eviction: list[Lease] = []
            async with self._lock:
                now = self._clock.monotonic()
                expired_in_eviction = self._evict_expired_for_resource(rkey, now)

                # Check if this holder already has a lease on this resource
                existing = self._by_resource.get(rkey, {}).get(holder_id)
                if existing is not None and not existing.is_expired(now):
                    if existing.state == state:
                        # Idempotent: same holder, same state — extend TTL
                        updated = replace(existing, expires_at=now + ttl)
                        self._store_lease_unlocked(rkey, updated)
                        self._acquire_count += 1
                        return updated
                    else:
                        # Upgrade/downgrade: remove old lease first
                        self._remove_lease_unlocked(rkey, holder_id)

                # Check for conflicts with other holders
                conflicts = self._get_conflicts_unlocked(rkey, holder_id, state, now)

                if not conflicts:
                    # No conflicts — grant immediately
                    gen = self._next_generation(rkey)
                    lease = Lease(
                        resource_id=resource_id,
                        holder_id=holder_id,
                        state=state,
                        generation=gen,
                        granted_at=now,
                        expires_at=now + ttl,
                    )
                    self._store_lease_unlocked(rkey, lease)
                    self._acquire_count += 1
                    logger.debug(
                        "[LeaseManager] Granted %s on %s to %s (gen=%d)",
                        state.value,
                        resource_id,
                        holder_id,
                        gen,
                    )
                    return lease

                # Conflicts exist — check timeout BEFORE revoking to avoid
                # destructive non-blocking acquire (codex finding #1: timeout=0
                # must not evict holders and then fail to grant).
                if timeout <= 0:
                    self._timeout_count += 1
                    return None

                # Revoke conflicting holders (DFUSE Algorithm 2)
                revoked: list[Lease] = []
                for conflict in conflicts:
                    removed = self._remove_lease_unlocked(rkey, conflict.holder_id)
                    if removed is not None:
                        revoked.append(removed)
                        self._revoke_count += 1

            # Invoke callbacks outside the lock to avoid deadlock
            if expired_in_eviction:
                await self._invoke_callbacks(expired_in_eviction, "expired")
            if revoked:
                await self._invoke_callbacks(revoked, "conflict")

            # Brief yield to let other tasks run, then retry
            await asyncio.sleep(min(retry_interval, max(0, deadline - self._clock.monotonic())))
            retry_interval = min(retry_interval * _BACKOFF_MULTIPLIER, _MAX_RETRY_INTERVAL)

    async def validate(
        self,
        resource_id: str,
        holder_id: str,
    ) -> Lease | None:
        """Return the active lease if still valid for this holder/resource pair."""
        rkey = self._resource_key(resource_id)
        now = self._clock.monotonic()
        expired: list[Lease] = []
        async with self._lock:
            expired = self._evict_expired_for_resource(rkey, now)
            holders = self._by_resource.get(rkey)
            if not holders:
                result = None
            else:
                lease = holders.get(holder_id)
                result = None if (lease is None or lease.is_expired(now)) else lease
        if expired:
            await self._invoke_callbacks(expired, "expired")
        return result

    async def revoke(
        self,
        resource_id: str,
        *,
        holder_id: str | None = None,
    ) -> list[Lease]:
        """Revoke leases on a resource."""
        rkey = self._resource_key(resource_id)
        revoked: list[Lease] = []

        async with self._lock:
            holders = self._by_resource.get(rkey)
            if not holders:
                return []

            if holder_id is not None:
                removed = self._remove_lease_unlocked(rkey, holder_id)
                if removed is not None:
                    revoked.append(removed)
                    self._revoke_count += 1
            else:
                # Revoke all holders
                for hid in list(holders.keys()):
                    removed = self._remove_lease_unlocked(rkey, hid)
                    if removed is not None:
                        revoked.append(removed)
                        self._revoke_count += 1

        if revoked:
            await self._invoke_callbacks(revoked, "explicit")
            logger.debug(
                "[LeaseManager] Revoked %d lease(s) on %s",
                len(revoked),
                resource_id,
            )
        return revoked

    async def revoke_holder(self, holder_id: str) -> list[Lease]:
        """Revoke all leases owned by a holder."""
        revoked: list[Lease] = []

        async with self._lock:
            rkeys = list(self._by_holder.get(holder_id, set()))
            for rkey in rkeys:
                removed = self._remove_lease_unlocked(rkey, holder_id)
                if removed is not None:
                    revoked.append(removed)
                    self._revoke_count += 1

        if revoked:
            await self._invoke_callbacks(revoked, "holder_disconnect")
            logger.debug(
                "[LeaseManager] Revoked %d lease(s) for holder %s",
                len(revoked),
                holder_id,
            )
        return revoked

    async def extend(
        self,
        resource_id: str,
        holder_id: str,
        *,
        ttl: float = DEFAULT_TTL,
    ) -> Lease | None:
        """Heartbeat / extend an existing lease."""
        rkey = self._resource_key(resource_id)
        now = self._clock.monotonic()

        async with self._lock:
            holders = self._by_resource.get(rkey)
            if not holders:
                return None
            lease = holders.get(holder_id)
            if lease is None or lease.is_expired(now):
                # Expired — clean up
                if lease is not None:
                    self._remove_lease_unlocked(rkey, holder_id)
                return None

            updated = replace(lease, expires_at=now + ttl)
            self._store_lease_unlocked(rkey, updated)
            self._extend_count += 1
            return updated

    # -- diagnostics ----------------------------------------------------------

    async def leases_for_resource(self, resource_id: str) -> list[Lease]:
        """Return all active leases for a resource."""
        rkey = self._resource_key(resource_id)
        now = self._clock.monotonic()

        async with self._lock:
            expired = self._evict_expired_for_resource(rkey, now)
            holders = self._by_resource.get(rkey)
            result = list(holders.values()) if holders else []
        if expired:
            await self._invoke_callbacks(expired, "expired")
        return result

    # -- lifecycle & observability --------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Return operational statistics."""
        async with self._lock:
            active_leases = sum(len(h) for h in self._by_resource.values())
            active_resources = len(self._by_resource)

        return {
            "acquire_count": self._acquire_count,
            "revoke_count": self._revoke_count,
            "extend_count": self._extend_count,
            "timeout_count": self._timeout_count,
            "callback_error_count": self._callback_error_count,
            "active_leases": active_leases,
            "active_resources": active_resources,
        }

    async def force_revoke(self, resource_id: str) -> list[Lease]:
        """Force-revoke all holders without invoking callbacks (admin/debug)."""
        rkey = self._resource_key(resource_id)
        revoked: list[Lease] = []

        async with self._lock:
            holders = self._by_resource.get(rkey)
            if not holders:
                return []
            for hid in list(holders.keys()):
                removed = self._remove_lease_unlocked(rkey, hid)
                if removed is not None:
                    revoked.append(removed)
                    self._revoke_count += 1

        if revoked:
            logger.debug(
                "[LeaseManager] Force-revoked %d lease(s) on %s",
                len(revoked),
                resource_id,
            )
        return revoked

    async def close(self) -> None:
        """Shut down background tasks. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._sweep_task is not None and not self._sweep_task.done():
            try:
                self._sweep_task.cancel()
                await self._sweep_task
            except (asyncio.CancelledError, RuntimeError):
                pass  # RuntimeError if event loop is closing
        logger.debug("[LeaseManager] Closed")

    # -- callback registration ------------------------------------------------

    def register_revocation_callback(
        self,
        callback_id: str,
        callback: RevocationCallback,
    ) -> None:
        """Register an async callback invoked on lease revocation."""
        for cid, _ in self._callbacks:
            if cid == callback_id:
                return  # Already registered
        self._callbacks.append((callback_id, callback))
        logger.debug("[LeaseManager] Registered callback: %s", callback_id)

    def unregister_revocation_callback(self, callback_id: str) -> bool:
        """Remove a previously registered callback."""
        for i, (cid, _) in enumerate(self._callbacks):
            if cid == callback_id:
                self._callbacks.pop(i)
                logger.debug("[LeaseManager] Unregistered callback: %s", callback_id)
                return True
        return False
