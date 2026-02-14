"""VFS router kernel protocol (Nexus Lego Architecture, Issue #1383).

Defines the contract for virtual path routing to storage backends.
Existing implementation: ``nexus.core.router.PathRouter`` (sync).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Part 2
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.backends.backend import Backend


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """Result of resolving a virtual path through the VFS router.

    Returned by ``route()`` — combines the matched mount info with the
    backend-relative path for the specific resolution.

    Attributes:
        virtual_path: The original virtual path that was resolved.
        backend_path: Path relative to the matched backend root.
        mount_point: The mount point that matched.
        readonly: Whether the mount is read-only.
        zone_id: Zone/organization ID associated with the route.
    """

    virtual_path: str
    backend_path: str
    mount_point: str
    readonly: bool
    zone_id: str | None


@dataclass(frozen=True, slots=True)
class MountInfo:
    """Describes a registered mount point.

    Returned by ``list_mounts()`` — carries mount-level metadata without
    exposing the backend object or route-resolution details.

    Attributes:
        mount_point: Virtual path prefix (e.g. "/workspace").
        priority: Mount priority (higher = preferred on overlap).
        readonly: Whether the mount is read-only.
    """

    mount_point: str
    priority: int
    readonly: bool


@runtime_checkable
class VFSRouterProtocol(Protocol):
    """Kernel contract for virtual path routing.

    All methods are async.  The existing ``PathRouter`` (sync) conforms once
    wrapped with an async adapter.
    """

    async def route(
        self,
        virtual_path: str,
        *,
        zone_id: str | None = None,
        is_admin: bool = False,
        check_write: bool = False,
    ) -> ResolvedPath: ...

    async def add_mount(
        self,
        mount_point: str,
        backend: Backend,
        *,
        priority: int = 0,
        readonly: bool = False,
    ) -> None: ...

    async def remove_mount(self, mount_point: str) -> bool: ...

    async def list_mounts(self) -> list[MountInfo]: ...
