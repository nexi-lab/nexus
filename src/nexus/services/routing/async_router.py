"""Async wrapper for PathRouter (Issue #1440).

Thin adapter that wraps the sync ``PathRouter`` to satisfy
``VFSRouterProtocol`` (all-async signatures).  All ``PathRouter``
methods are pure CPU (in-memory data structures), so they use
**direct calls** (no ``asyncio.to_thread``).

References:
    - Issue #1440: Async wrappers for 4 sync kernel protocols
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.core.protocols.vfs_router import MountInfo, ResolvedPath

if TYPE_CHECKING:
    from nexus.core.router import MountConfig, PathRouter, RouteResult


def _to_resolved_path(
    result: RouteResult,
    virtual_path: str,
    zone_id: str | None,
) -> ResolvedPath:
    """Convert a ``RouteResult`` to the protocol-level ``ResolvedPath``."""
    return ResolvedPath(
        virtual_path=virtual_path,
        backend_path=result.backend_path,
        mount_point=result.mount_point,
        readonly=result.readonly,
        zone_id=zone_id,
    )


def _to_mount_info(config: MountConfig) -> MountInfo:
    """Convert a ``MountConfig`` to the protocol-level ``MountInfo``."""
    return MountInfo(
        mount_point=config.mount_point,
        priority=config.priority,
        readonly=config.readonly,
    )


class AsyncVFSRouter:
    """Async adapter for ``PathRouter`` conforming to ``VFSRouterProtocol``.

    All methods are **direct calls** (no ``to_thread``) because
    ``PathRouter`` operates on in-memory data structures with no I/O.
    """

    def __init__(self, inner: PathRouter) -> None:
        self._inner = inner

    async def route(
        self,
        virtual_path: str,
        *,
        zone_id: str | None = None,
        is_admin: bool = False,
        check_write: bool = False,
    ) -> ResolvedPath:
        result = self._inner.route(
            virtual_path,
            zone_id=zone_id,
            is_admin=is_admin,
            check_write=check_write,
        )
        return _to_resolved_path(result, virtual_path, zone_id)

    async def add_mount(
        self,
        mount_point: str,
        backend: Any,
        *,
        priority: int = 0,
        readonly: bool = False,
    ) -> None:
        self._inner.add_mount(
            mount_point,
            backend,
            priority=priority,
            readonly=readonly,
        )

    async def remove_mount(self, mount_point: str) -> bool:
        return self._inner.remove_mount(mount_point)

    async def list_mounts(self) -> list[MountInfo]:
        configs = self._inner.list_mounts()
        return [_to_mount_info(c) for c in configs]
