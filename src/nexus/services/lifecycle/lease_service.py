"""Lease lifecycle service — service owner for LeaseManager (Issue #3407).

Owns the ``LocalLeaseManager`` lifecycle: construction, zone-scoped wiring,
and shutdown.  Acts as the single entry point for lease operations in the
service layer.

Placement follows the same pattern as ``ZoneLifecycleService`` —
dependency-injected, zone_id keyword-only at construction,
async lifecycle management.

References:
    - Issue #3407: Common LeaseManager utility
    - contracts/protocols/lease.py: LeaseManagerProtocol
    - lib/lease.py: LocalLeaseManager implementation
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.lease import (
    Lease,
    LeaseManagerProtocol,
    LeaseState,
    RevocationCallback,
)
from nexus.lib.lease import LocalLeaseManager, SystemClock

if TYPE_CHECKING:
    from nexus.contracts.protocols.lease import Clock

logger = logging.getLogger(__name__)


class LeaseService:
    """Service-layer owner for lease management.

    Provides a thin wrapper around ``LeaseManagerProtocol`` with:
    - Construction-time zone binding
    - Hot-swap support (local -> distributed) via ``upgrade_manager()``
    - Lifecycle management (close on shutdown)

    Example::

        svc = LeaseService(zone_id="us-east-1")
        lease = await svc.acquire("file:123", "agent-A", LeaseState.SHARED_READ)
        await svc.close()
    """

    def __init__(
        self,
        *,
        zone_id: str = ROOT_ZONE_ID,
        clock: Clock | None = None,
        manager: LeaseManagerProtocol | None = None,
    ) -> None:
        self._zone_id = zone_id
        if manager is not None:
            self._manager: LeaseManagerProtocol = manager
        else:
            self._manager = LocalLeaseManager(
                zone_id=zone_id,
                clock=clock or SystemClock(),
            )
        logger.info(
            "[LeaseService] Initialized (zone=%s, manager=%s)",
            zone_id,
            type(self._manager).__name__,
        )

    # ------------------------------------------------------------------
    # Manager access & hot-swap
    # ------------------------------------------------------------------

    @property
    def manager(self) -> LeaseManagerProtocol:
        """Access the underlying lease manager."""
        return self._manager

    def upgrade_manager(self, manager: LeaseManagerProtocol) -> None:
        """Hot-swap the lease manager (e.g. local -> distributed at link time).

        The old manager is NOT closed — the caller is responsible for
        migrating state if needed.
        """
        old_type = type(self._manager).__name__
        self._manager = manager
        logger.info(
            "[LeaseService] Manager upgraded: %s -> %s",
            old_type,
            type(manager).__name__,
        )

    # ------------------------------------------------------------------
    # Delegated API — thin pass-through
    # ------------------------------------------------------------------

    async def acquire(
        self,
        resource_id: str,
        holder_id: str,
        state: LeaseState,
        *,
        ttl: float = 30.0,
        timeout: float = 30.0,
    ) -> Lease | None:
        """Acquire a lease. See ``LeaseManagerProtocol.acquire``."""
        return await self._manager.acquire(resource_id, holder_id, state, ttl=ttl, timeout=timeout)

    async def validate(
        self,
        resource_id: str,
        holder_id: str,
    ) -> Lease | None:
        """Validate an active lease. See ``LeaseManagerProtocol.validate``."""
        return await self._manager.validate(resource_id, holder_id)

    async def revoke(
        self,
        resource_id: str,
        *,
        holder_id: str | None = None,
    ) -> list[Lease]:
        """Revoke leases. See ``LeaseManagerProtocol.revoke``."""
        return await self._manager.revoke(resource_id, holder_id=holder_id)

    async def revoke_holder(self, holder_id: str) -> list[Lease]:
        """Revoke all leases for a holder. See ``LeaseManagerProtocol.revoke_holder``."""
        return await self._manager.revoke_holder(holder_id)

    async def extend(
        self,
        resource_id: str,
        holder_id: str,
        *,
        ttl: float = 30.0,
    ) -> Lease | None:
        """Extend a lease. See ``LeaseManagerProtocol.extend``."""
        return await self._manager.extend(resource_id, holder_id, ttl=ttl)

    async def leases_for_resource(self, resource_id: str) -> list[Lease]:
        """List leases for a resource. See ``LeaseManagerProtocol.leases_for_resource``."""
        return await self._manager.leases_for_resource(resource_id)

    async def stats(self) -> dict[str, Any]:
        """Get statistics. See ``LeaseManagerProtocol.stats``."""
        return await self._manager.stats()

    async def force_revoke(self, resource_id: str) -> list[Lease]:
        """Force-revoke. See ``LeaseManagerProtocol.force_revoke``."""
        return await self._manager.force_revoke(resource_id)

    def register_revocation_callback(
        self,
        callback_id: str,
        callback: RevocationCallback,
    ) -> None:
        """Register a revocation callback. See ``LeaseManagerProtocol``."""
        self._manager.register_revocation_callback(callback_id, callback)

    def unregister_revocation_callback(self, callback_id: str) -> bool:
        """Unregister a callback. See ``LeaseManagerProtocol``."""
        return self._manager.unregister_revocation_callback(callback_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Shut down the lease manager. Idempotent."""
        await self._manager.close()
        logger.info("[LeaseService] Closed")
