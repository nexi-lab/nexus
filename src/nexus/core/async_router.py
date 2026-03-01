"""Async wrapper for PathRouter (Issue #1440).

Thin adapter that wraps the sync ``PathRouter`` to satisfy
``VFSRouterProtocol`` (all-async signatures).  All ``PathRouter``
methods are pure CPU (in-memory data structures), so they use
**direct calls** (no ``asyncio.to_thread``).

References:
    - Issue #1440: Async wrappers for 4 sync kernel protocols
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from typing import TYPE_CHECKING

from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath

if TYPE_CHECKING:
    from nexus.core.object_store import ObjectStoreABC
    from nexus.core.router import PathRouter, RouteResult


def _to_resolved_path(
    result: "RouteResult",
    virtual_path: str,
) -> ResolvedPath:
    """Convert a ``RouteResult`` to the protocol-level ``ResolvedPath``."""
    return ResolvedPath(
        virtual_path=virtual_path,
        backend_path=result.backend_path,
        mount_point=result.mount_point,
        readonly=result.readonly,
    )


class AsyncVFSRouter:
    """Async adapter for ``PathRouter`` conforming to ``VFSRouterProtocol``.

    All methods are **direct calls** (no ``to_thread``) because
    ``PathRouter`` operates on in-memory data structures with no I/O.
    """

    def __init__(self, inner: "PathRouter") -> None:
        self._inner = inner

    async def route(
        self,
        virtual_path: str,
        *,
        is_admin: bool = False,
        check_write: bool = False,
    ) -> ResolvedPath:
        result = self._inner.route(
            virtual_path,
            is_admin=is_admin,
            check_write=check_write,
        )
        return _to_resolved_path(result, virtual_path)

    async def add_mount(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
    ) -> None:
        self._inner.add_mount(
            mount_point,
            backend,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
        )

    async def remove_mount(self, mount_point: str) -> bool:
        return self._inner.remove_mount(mount_point)

    async def list_mounts(self) -> list[MountInfo]:
        return self._inner.list_mounts()

    async def get_mount_points(self) -> list[str]:
        return self._inner.get_mount_points()
