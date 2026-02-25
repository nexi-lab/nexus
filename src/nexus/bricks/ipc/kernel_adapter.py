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

import asyncio
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

    async def read(self, path: str, zone_id: str) -> bytes:
        self._require_bound()
        ctx = self._ctx(zone_id)
        return await asyncio.to_thread(self._nx.read, path, context=ctx)

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await asyncio.to_thread(self._nx.write, path, data, context=ctx)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        self._require_bound()
        ctx = self._ctx(zone_id)
        raw: list[Any] = await asyncio.to_thread(self._nx.list, path, recursive=False, context=ctx)
        # NexusFS.list returns full paths or dicts; strip to filenames
        prefix = path.rstrip("/") + "/"
        result: list[str] = []
        for entry in raw:
            p = (
                str(entry.get("path", entry.get("name", "")))
                if isinstance(entry, dict)
                else str(entry)
            )
            if p.startswith(prefix):
                p = p[len(prefix) :]
            # Only direct children (no nested '/')
            if p and "/" not in p:
                result.append(p)
        return result

    async def count_dir(self, path: str, zone_id: str) -> int:
        entries = await self.list_dir(path, zone_id)
        return len(entries)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await asyncio.to_thread(self._nx.rename, src, dst, context=ctx)

    async def mkdir(self, path: str, zone_id: str) -> None:
        self._require_bound()
        ctx = self._ctx(zone_id)
        await asyncio.to_thread(self._nx.mkdir, path, parents=True, exist_ok=True, context=ctx)

    async def exists(self, path: str, zone_id: str) -> bool:
        self._require_bound()
        ctx = self._ctx(zone_id)
        return await asyncio.to_thread(self._nx.exists, path, context=ctx)
