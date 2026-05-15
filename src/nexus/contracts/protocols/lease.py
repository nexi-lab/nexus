"""Lease manager protocol — shared foundation for DFUSE-inspired optimizations.

Defines a narrow, policy-neutral lease primitive for coordinating shared-read
vs exclusive-write access to resources.  Multiple higher-level components
(FUSE cache coherence, ReBAC permission fast-paths, lease-aware cache
eviction, cross-zone coordination) consume this contract without
reimplementing lease semantics.

The protocol intentionally does NOT embed authorization-specific fields.
A permission fast-path may consume a valid lease, but permission policy
belongs in the permission layer.

Design principles:
    1. Narrow primitive, policy above it.
    2. Zone/topology bound at construction, not per-method.
    3. Backends (local, distributed, Raft) may have different consistency
       guarantees — the protocol defines the common contract only.
    4. Lease lifecycle is NOT a file event (use service-level observers).

Implementation: ``nexus.lib.lease.LocalLeaseManager``
Storage Affinity: **CacheStore** (ephemeral, TTL-based)

References:
    - DFUSE paper: https://arxiv.org/abs/2503.18191
    - DFUSE S3.1: lease states, Algorithm 1/2: AcquireLease/GrantLease
    - Issue #3407: Common LeaseManager utility
    - Issue #3394, #3396, #3397, #3398, #3400: dependent consumers

Convention (Issue #1291):
    - All protocols use @runtime_checkable for test-time isinstance() checks.
    - Do NOT use isinstance(obj, Protocol) in production hot paths.
    - All data classes use @dataclass(frozen=True, slots=True).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

# =============================================================================
# Clock protocol — injectable time source for testability
# =============================================================================


@runtime_checkable
class Clock(Protocol):
    """Monotonic time source.

    Production code uses ``SystemClock``; tests inject ``ManualClock``
    to advance time deterministically without ``time.sleep()``.
    """

    def monotonic(self) -> float:
        """Return the current monotonic time in seconds."""
        ...


# =============================================================================
# Data model
# =============================================================================


class LeaseState(StrEnum):
    """Lease access mode — DFUSE S3.1 shared-read / exclusive-write."""

    SHARED_READ = "shared"
    EXCLUSIVE_WRITE = "exclusive"


@dataclass(frozen=True, slots=True)
class Lease:
    """Immutable grant receipt for a lease.

    A Lease is a snapshot of what was granted.  It does NOT track live
    validity — callers must use ``LeaseManagerProtocol.validate()`` or
    compare ``expires_at`` against the clock to check current validity.

    The ``generation`` field is a monotonically increasing fencing token
    per resource.  Protected resources can use it to reject stale writes
    from holders whose lease has expired but who haven't yet noticed.
    """

    resource_id: str
    """Stable resource identity (avoid path-only if rename matters)."""

    holder_id: str
    """Agent ID, mount ID, worker ID, etc."""

    state: LeaseState
    """Access mode granted."""

    generation: int
    """Monotonically increasing fencing token per resource."""

    granted_at: float
    """Monotonic timestamp when the lease was granted."""

    expires_at: float
    """Monotonic timestamp when the lease expires (without renewal)."""

    def is_expired(self, now: float) -> bool:
        """Check if this lease has expired at the given monotonic time."""
        return now >= self.expires_at


# =============================================================================
# Callback type for revocation notifications
# =============================================================================

RevocationCallback = Callable[[Lease, str], Awaitable[None]]
"""Async callback invoked when a lease is revoked.

Args:
    lease: The lease being revoked.
    reason: Human-readable reason (``"conflict"``, ``"explicit"``, ``"expired"``).
"""


# =============================================================================
# Protocol
# =============================================================================


@runtime_checkable
class LeaseManagerProtocol(Protocol):
    """Structural interface for lease management.

    Supports shared-read (multiple concurrent holders) and exclusive-write
    (single holder) with DFUSE-style conflict resolution: acquiring a
    conflicting lease revokes existing holders before granting.

    zone_id is bound at construction — callers never pass it per-method.

    Example::

        mgr = LocalLeaseManager(zone_id="us-east-1")
        lease = await mgr.acquire("file:123", "agent-A", LeaseState.SHARED_READ)
        if lease:
            # ... use resource ...
            await mgr.revoke(lease.resource_id, holder_id="agent-A")
    """

    # -- core operations ------------------------------------------------------

    async def acquire(
        self,
        resource_id: str,
        holder_id: str,
        state: LeaseState,
        *,
        ttl: float = 30.0,
        timeout: float = 30.0,
    ) -> Lease | None:
        """Acquire a lease, blocking until conflicts are resolved or timeout.

        Follows DFUSE Algorithm 2 (GrantLease): if the requested state
        conflicts with existing holders, their leases are revoked (with
        callbacks invoked) before the new lease is granted.

        Args:
            resource_id: Stable resource identity.
            holder_id: Identity of the requesting holder.
            state: Desired lease mode.
            ttl: Lease time-to-live in seconds.
            timeout: Maximum time to wait for conflict resolution (seconds).
                     Pass ``0`` for non-blocking (returns ``None`` on conflict).

        Returns:
            A ``Lease`` grant receipt if acquired, ``None`` on timeout.
        """
        ...

    async def validate(
        self,
        resource_id: str,
        holder_id: str,
    ) -> Lease | None:
        """Return the active lease if still valid for this holder/resource pair.

        Returns:
            The current ``Lease`` if valid, ``None`` if expired or not held.
        """
        ...

    async def revoke(
        self,
        resource_id: str,
        *,
        holder_id: str | None = None,
    ) -> list[Lease]:
        """Revoke leases on a resource.

        If ``holder_id`` is given, revoke only that holder's lease.
        If ``None``, revoke all holders for the resource.

        Returns:
            List of revoked ``Lease`` objects.
        """
        ...

    async def revoke_holder(self, holder_id: str) -> list[Lease]:
        """Revoke all leases owned by a holder (e.g. on disconnect).

        Returns:
            List of revoked ``Lease`` objects.
        """
        ...

    async def extend(
        self,
        resource_id: str,
        holder_id: str,
        *,
        ttl: float = 30.0,
    ) -> Lease | None:
        """Heartbeat / extend an existing lease.

        Returns:
            Updated ``Lease`` with new ``expires_at``, or ``None`` if
            the lease has already expired or does not exist.
        """
        ...

    # -- diagnostics ----------------------------------------------------------

    async def leases_for_resource(self, resource_id: str) -> list[Lease]:
        """Return all active leases for a resource (diagnostics)."""
        ...

    # -- lifecycle & observability --------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Return operational statistics.

        Expected keys: ``acquire_count``, ``revoke_count``, ``extend_count``,
        ``timeout_count``, ``active_leases``, ``active_resources``.
        """
        ...

    async def force_revoke(self, resource_id: str) -> list[Lease]:
        """Force-revoke all holders without invoking callbacks (admin/debug).

        Returns:
            List of force-revoked ``Lease`` objects.
        """
        ...

    async def close(self) -> None:
        """Shut down background tasks and release resources.

        Idempotent — safe to call multiple times.
        """
        ...

    # -- callback registration ------------------------------------------------

    def register_revocation_callback(
        self,
        callback_id: str,
        callback: RevocationCallback,
    ) -> None:
        """Register an async callback invoked on lease revocation.

        Callbacks run concurrently with a per-callback timeout.
        A failing callback does not prevent revocation.

        Args:
            callback_id: Unique identifier (deduplicated on register).
            callback: Async callable ``(lease, reason) -> None``.
        """
        ...

    def unregister_revocation_callback(self, callback_id: str) -> bool:
        """Remove a previously registered callback.

        Returns:
            ``True`` if the callback was found and removed.
        """
        ...
