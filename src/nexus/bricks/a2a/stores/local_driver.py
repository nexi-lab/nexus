"""Local-filesystem storage driver for A2A task persistence.

Implements the ``VFSOperations`` protocol using plain filesystem
operations.  This driver is used when the server starts without a
VFS-backed IPC layer (e.g., ``nexusd`` with ``--data-dir``).

File operations are wrapped in ``asyncio.to_thread()`` to prevent
blocking the event loop.
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class LocalStorageDriver:
    """Local-filesystem ``VFSOperations`` implementation.

    Parameters
    ----------
    root:
        Root directory for file storage.  All paths are relative to this.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str, zone_id: str) -> Path:
        """Resolve a virtual path to a zone-scoped absolute filesystem path.

        All paths are scoped under ``<root>/<zone_id>/`` so that each
        zone's data is isolated on disk.
        """
        clean = path.lstrip("/")
        zone_root = (self._root / zone_id).resolve()
        resolved = (zone_root / clean).resolve()
        # Guard against path traversal
        try:
            resolved.relative_to(zone_root)
        except ValueError:
            raise ValueError(f"Path traversal blocked: {path}") from None
        return resolved

    async def read(self, path: str, zone_id: str) -> bytes:
        real = self._resolve(path, zone_id)
        try:
            return await asyncio.to_thread(real.read_bytes)
        except FileNotFoundError:
            raise FileNotFoundError(f"Not found: {path}") from None

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        real = self._resolve(path, zone_id)
        await asyncio.to_thread(self._write_sync, real, data)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        real = self._resolve(path, zone_id)
        if not real.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")
        entries = await asyncio.to_thread(list, real.iterdir())
        return [e.name for e in entries]

    async def count_dir(self, path: str, zone_id: str) -> int:
        entries = await self.list_dir(path, zone_id)
        return len(entries)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        real_src = self._resolve(src, zone_id)
        real_dst = self._resolve(dst, zone_id)
        if not real_src.exists():
            raise FileNotFoundError(f"Not found: {src}")
        await asyncio.to_thread(real_src.rename, real_dst)

    async def mkdir(self, path: str, zone_id: str) -> None:
        real = self._resolve(path, zone_id)
        await asyncio.to_thread(real.mkdir, parents=True, exist_ok=True)

    async def exists(self, path: str, zone_id: str) -> bool:
        real = self._resolve(path, zone_id)
        return await asyncio.to_thread(real.exists)

    @staticmethod
    def _write_sync(path: Path, data: bytes) -> None:
        """Write bytes to file atomically, creating parent dirs as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file + rename prevents partial writes on crash
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
