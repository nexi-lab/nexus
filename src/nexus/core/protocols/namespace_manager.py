"""Namespace manager kernel protocol (Nexus Lego Architecture, Issue #1383).

Defines the contract for per-subject namespace visibility.
Existing implementation: ``nexus.core.namespace_manager.NamespaceManager`` (sync).

No ``mount()`` / ``unmount()`` — the existing implementation rebuilds from
ReBAC grants, not explicit mount calls (pragmatic 5A decision).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Part 2
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class NamespaceMount:
    """A namespace mount visible to a subject.

    Distinct from the existing ``MountEntry`` (which only has ``virtual_path``).
    Includes subject identity so mount tables can be compared across subjects.

    Attributes:
        virtual_path: Virtual path visible to the subject.
        subject_type: Subject type (e.g. "user", "agent").
        subject_id: Subject identifier.
        zone_id: Zone/organization ID for multi-zone isolation.
    """

    virtual_path: str
    subject_type: str
    subject_id: str
    zone_id: str | None


@runtime_checkable
class NamespaceManagerProtocol(Protocol):
    """Kernel contract for per-subject namespace visibility.

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
    ) -> list[NamespaceMount]: ...

    async def invalidate(self, subject: tuple[str, str]) -> None: ...
