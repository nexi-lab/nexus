"""KernelVFSAdapter — async adapter wrapping sync NexusFS for IPC.

Routes all IPC storage operations through the kernel VFS (NexusFS),
gaining PathRouter routing, ReBAC permission checks, MetastoreABC
metadata tracking, EventLog auditing, content caching, and Raft
replication — none of which the old RecordStoreStorageDriver provided.

Uses a lazy-bind pattern: created with ``zone_id`` only during
``_create_bricks()``, then ``bind(nexus_fs)`` is called once NexusFS
exists in ``_boot_wired_services()``.

Issue: #1178
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.types import VFSOperations

logger = logging.getLogger(__name__)


@dataclass
class KernelVFSAdapter:
    """Async IPC storage adapter delegating to the kernel VFS (NexusFS).

    Satisfies the ``VFSOperations`` protocol from
    ``nexus.bricks.ipc.protocols`` (async methods with ``zone_id``).

    Parameters
    ----------
    zone_id:
        Default zone for constructing ``OperationContext``.
    """

    zone_id: str
    _nx: Any = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Lazy binding
    # ------------------------------------------------------------------

    def bind(self, nexus_fs: "VFSOperations") -> None:
        """Bind to a live NexusFS instance (called after kernel init)."""
        self._nx = nexus_fs
        logger.info("[IPC] KernelVFSAdapter bound to NexusFS (zone=%s)", self.zone_id)

    @property
    def is_bound(self) -> bool:
        return self._nx is not None

    # ------------------------------------------------------------------
    # Context helper
    # ------------------------------------------------------------------

    def _ctx(self, zone_id: str) -> Any:
        """Build an ``OperationContext`` for IPC system operations."""
        from nexus.contracts.types import OperationContext

        return OperationContext(
            user_id="system",
            groups=[],
            zone_id=zone_id,
            is_system=True,
        )

    def _require_bound(self) -> None:
        if self._nx is None:
            raise RuntimeError("KernelVFSAdapter.bind(nexus_fs) has not been called yet")

    # ------------------------------------------------------------------
    # VFSOperations protocol (async)
    # ------------------------------------------------------------------

    async def sys_read(self, path: str, zone_id: str) -> bytes:
        self._require_bound()
        ctx = self._ctx(zone_id)
        result: bytes = await self._nx.sys_read(path, context=ctx)
        return result

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await self._nx.write(path, data, context=ctx)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:  # noqa: ARG002
        self._require_bound()
        # Route through PathRouter directly to the LocalConnector backend.
        # This bypasses the metadata layer (FederatedMetadataProxy) whose
        # Raft prefix scan may not index entries under the /agents mount.
        import asyncio

        route = self._nx.router.route(path, is_admin=True, check_write=False)
        raw: list[str] = await asyncio.to_thread(route.backend.list_dir, route.backend_path)
        return [name for name in raw if "/" not in name]

    async def count_dir(self, path: str, zone_id: str) -> int:
        entries = await self.list_dir(path, zone_id)
        return len(entries)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await self._nx.rename(src, dst, context=ctx)

    async def sys_mkdir(self, path: str, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await self._nx.sys_mkdir(path, parents=True, exist_ok=True, context=ctx)

    # Alias for backward compatibility
    mkdir = sys_mkdir

    async def sys_access(self, path: str, zone_id: str) -> bool:  # noqa: ARG002
        self._require_bound()
        import asyncio

        try:
            route = self._nx.router.route(path, is_admin=True, check_write=False)
            return await asyncio.to_thread(route.backend.exists, route.backend_path)
        except (FileNotFoundError, KeyError):
            return False

    # Alias for backward compatibility
    exists = sys_access
