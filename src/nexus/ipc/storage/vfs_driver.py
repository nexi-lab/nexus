"""VFS-backed storage driver for IPC messages.

Thin adapter that delegates all operations to a ``VFSOperations`` instance.
When the VFS is backed by the real Nexus VFS Router, operations automatically
gain ReBAC permission checks, EventLog auditing, and caching.
"""

from __future__ import annotations

from nexus.ipc.protocols import VFSOperations


class VFSStorageDriver:
    """Delegates IPC storage to the Nexus VFS layer.

    Args:
        vfs: The VFS operations instance to delegate to.
    """

    def __init__(self, vfs: VFSOperations) -> None:
        self._vfs = vfs

    async def read(self, path: str, zone_id: str) -> bytes:
        return await self._vfs.read(path, zone_id)

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        await self._vfs.write(path, data, zone_id)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        return await self._vfs.list_dir(path, zone_id)

    async def count_dir(self, path: str, zone_id: str) -> int:
        return await self._vfs.count_dir(path, zone_id)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        await self._vfs.rename(src, dst, zone_id)

    async def mkdir(self, path: str, zone_id: str) -> None:
        await self._vfs.mkdir(path, zone_id)

    async def exists(self, path: str, zone_id: str) -> bool:
        return await self._vfs.exists(path, zone_id)
