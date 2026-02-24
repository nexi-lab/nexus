"""VFS router kernel protocol (Issue #1383).

Defines the contract for virtual path routing to storage backends.
Existing implementation: ``nexus.core.router.PathRouter`` (sync, metastore-backed).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.object_store import ObjectStoreABC


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
    """

    virtual_path: str
    backend_path: str
    mount_point: str
    readonly: bool


@dataclass(frozen=True, slots=True)
class MountInfo:
    """Describes a registered mount point.

    Returned by ``list_mounts()`` — carries mount-level metadata without
    exposing the backend object or route-resolution details.

    Attributes:
        mount_point: Virtual path prefix (e.g. "/workspace").
        readonly: Whether the mount is read-only.
        admin_only: Whether the mount requires admin privileges.
    """

    mount_point: str
    readonly: bool
    admin_only: bool = False


@runtime_checkable
class VFSRouterProtocol(Protocol):
    """Kernel contract for virtual path routing.

    All methods are sync — metastore-backed PathRouter reads are ~5 μs
    via redb's Rust in-memory cache. No async overhead needed.
    """

    def route(
        self,
        virtual_path: str,
        *,
        is_admin: bool = False,
        check_write: bool = False,
    ) -> ResolvedPath: ...

    def add_mount(
        self,
        mount_point: str,
        backend: ObjectStoreABC,
        *,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
    ) -> None: ...

    def remove_mount(self, mount_point: str) -> bool: ...

    def list_mounts(self) -> list[MountInfo]: ...

    def get_mount_points(self) -> list[str]: ...
