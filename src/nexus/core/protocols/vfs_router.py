"""VFS router kernel protocol (Issue #1383).

Defines the contract for virtual path routing to storage backends.
Routing is handled by the Rust kernel (``PyKernel.route``) + DLC
for backend refs. PathRouter was deleted in §12 Phase F3.

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
    """

    virtual_path: str
    backend_path: str
    mount_point: str


@dataclass(frozen=True, slots=True)
class MountInfo:
    """Describes a registered mount point.

    Returned by ``list_mounts()`` — carries mount-level metadata without
    exposing route-resolution details.

    Attributes:
        mount_point: Virtual path prefix (e.g. "/workspace").
        backend: The storage backend instance (ObjectStoreABC), if available.
        priority: Mount priority for ordering (higher = checked first).
        conflict_strategy: Write-back conflict resolution strategy, or None.
    """

    mount_point: str
    status: str = "active"  # "active" or "stale"
    backend: "ObjectStoreABC | None" = None
    priority: int = 0
    conflict_strategy: str | None = None


@runtime_checkable
class VFSRouterProtocol(Protocol):
    """Kernel contract for virtual path routing.

    Routing is performed by the Rust kernel + DLC (kernel.route() for
    LPM, DLC for Python-side backend refs). Mount mutations go through
    DriverLifecycleCoordinator.

    All methods are sync — kernel reads are ~5 us via Rust in-memory
    cache. No async overhead needed.
    """

    def route(self, virtual_path: str) -> ResolvedPath: ...

    def list_mounts(self) -> list[MountInfo]: ...

    def get_mount_points(self) -> list[str]: ...
