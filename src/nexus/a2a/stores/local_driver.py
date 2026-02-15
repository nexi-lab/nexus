"""Local-filesystem storage driver for A2A task persistence.

Implements the ``IPCStorageDriver`` protocol using plain filesystem
operations.  This driver is used when the server starts without a
VFS-backed IPC layer (e.g., ``nexus serve`` with ``--data-dir``).

File operations are wrapped in ``asyncio.to_thread()`` to prevent
blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class LocalStorageDriver:
    """Local-filesystem ``IPCStorageDriver`` implementation.

    Parameters
    ----------
    root:
        Root directory for file storage.  All paths are relative to this.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        """Resolve a virtual path to an absolute filesystem path."""
        # Strip leading slash for relative resolution
        clean = path.lstrip("/")
        resolved = (self._root / clean).resolve()
        # Guard against path traversal using is_relative_to for safety
        try:
            resolved.relative_to(self._root.resolve())
        except ValueError:
            raise ValueError(f"Path traversal blocked: {path}") from None
        return resolved

    async def read(self, path: str, zone_id: str) -> bytes:  # noqa: ARG002
        real = self._resolve(path)
        try:
            return await asyncio.to_thread(real.read_bytes)
        except FileNotFoundError:
            raise FileNotFoundError(f"Not found: {path}") from None

    async def write(self, path: str, data: bytes, zone_id: str) -> None:  # noqa: ARG002
        real = self._resolve(path)
        await asyncio.to_thread(self._write_sync, real, data)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:  # noqa: ARG002
        real = self._resolve(path)
        if not real.is_dir():
            raise FileNotFoundError(f"Directory not found: {path}")
        entries = await asyncio.to_thread(list, real.iterdir())
        return [e.name for e in entries]

    async def count_dir(self, path: str, zone_id: str) -> int:
        entries = await self.list_dir(path, zone_id)
        return len(entries)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:  # noqa: ARG002
        real_src = self._resolve(src)
        real_dst = self._resolve(dst)
        if not real_src.exists():
            raise FileNotFoundError(f"Not found: {src}")
        await asyncio.to_thread(real_src.rename, real_dst)

    async def mkdir(self, path: str, zone_id: str) -> None:  # noqa: ARG002
        real = self._resolve(path)
        await asyncio.to_thread(real.mkdir, parents=True, exist_ok=True)

    async def exists(self, path: str, zone_id: str) -> bool:  # noqa: ARG002
        real = self._resolve(path)
        return await asyncio.to_thread(real.exists)

    @staticmethod
    def _write_sync(path: Path, data: bytes) -> None:
        """Write bytes to file atomically, creating parent dirs as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file + rename prevents partial writes on crash
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
