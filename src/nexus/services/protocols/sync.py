"""SyncService protocol (Issue #696).

Defines the contract for mount synchronisation.

Existing implementation: ``nexus.services.sync_service.SyncService``

References:
    - docs/design/KERNEL-ARCHITECTURE.md §1 (service DI)
"""

from typing import Protocol, runtime_checkable

from nexus.contracts.types import SyncContext, SyncResult


@runtime_checkable
class SyncServiceProtocol(Protocol):
    """Service contract for mount synchronisation."""

    def sync_mount(self, ctx: SyncContext) -> SyncResult: ...
