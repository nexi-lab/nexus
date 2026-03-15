"""Namespace manager service protocol (Issue #1383).

Defines the contract for per-subject namespace visibility.
Existing implementation: ``nexus.bricks.rebac.namespace_manager.NamespaceManager`` (sync).

No ``mount()`` / ``unmount()`` — the existing implementation rebuilds from
ReBAC grants, not explicit mount calls (pragmatic 5A decision).

Storage Affinity: **RecordStore + CacheStore** — ReBAC views from RecordStore,
    mount-table cache in CacheStore for fast lookups.

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class NamespaceManagerProtocol(Protocol):
    """Service contract for per-subject namespace visibility.

    All methods are async.  The existing ``NamespaceManager`` (sync) conforms
    once wrapped with an async adapter.
    """

    async def is_visible(
        self,
        subject: tuple[str, str],
        path: str,
        *,
        zone_id: str | None = None,
    ) -> bool: ...

    async def get_mount_table(
        self,
        subject: tuple[str, str],
        *,
        zone_id: str | None = None,
    ) -> list[Any]: ...

    async def invalidate(self, subject: tuple[str, str]) -> None: ...
