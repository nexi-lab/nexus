"""Direct local filesystem adapter for the playground TUI.

Bypasses the CAS backend and reads files directly from disk via pathlib.
This is what users expect when they run:

    nexus-fs playground local:///some/dir

They want to see the actual files on disk, not files written through
the NexusFS API.
"""

from __future__ import annotations

import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class LocalDirectFS:
    """Direct filesystem access — same interface as SlimNexusFS.

    Used by the playground for local:// URIs so users can browse
    real files without seeding through the API first.
    """

    def __init__(self, root: Path, mount_point: str) -> None:
        self._root = root.resolve()
        self._mount_point = mount_point

    def _to_real(self, virtual_path: str) -> Path:
        """Convert a virtual path to a real filesystem path."""
        # Strip mount point prefix to get relative path
        rel = virtual_path
        if rel.startswith(self._mount_point):
            rel = rel[len(self._mount_point) :]
        rel = rel.lstrip("/")
        return self._root / rel

    def _to_virtual(self, real_path: Path) -> str:
        """Convert a real path to a virtual mount path."""
        try:
            rel = real_path.resolve().relative_to(self._root)
            return f"{self._mount_point}/{rel}" if str(rel) != "." else self._mount_point
        except ValueError:
            return self._mount_point

    # -- SlimNexusFS-compatible interface --

    async def read(self, path: str) -> bytes:
        real = self._to_real(path)
        return real.read_bytes()

    async def read_range(self, path: str, start: int, end: int) -> bytes:
        real = self._to_real(path)
        with open(real, "rb") as f:
            f.seek(start)
            return f.read(end - start)

    async def write(self, path: str, content: bytes) -> dict[str, Any]:
        real = self._to_real(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(content)
        return {"path": path, "size": len(content)}

    async def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        real = self._to_real(path)
        if not real.is_dir():
            return []

        entries: list[dict[str, Any]] = []
        if recursive:
            for item in sorted(real.rglob("*")):
                entries.append(self._stat_entry(item))
        else:
            for item in sorted(real.iterdir()):
                entries.append(self._stat_entry(item))

        if detail:
            return entries
        return [e["path"] for e in entries]

    async def stat(self, path: str) -> dict[str, Any] | None:
        real = self._to_real(path)
        if not real.exists():
            return None
        return self._stat_entry(real)

    async def mkdir(self, path: str, parents: bool = True) -> None:
        real = self._to_real(path)
        real.mkdir(parents=parents, exist_ok=True)

    async def rmdir(self, path: str, recursive: bool = False) -> None:
        real = self._to_real(path)
        if recursive:
            import shutil

            shutil.rmtree(real)
        else:
            real.rmdir()

    async def delete(self, path: str) -> None:
        real = self._to_real(path)
        real.unlink()

    async def rename(self, old_path: str, new_path: str) -> None:
        self._to_real(old_path).rename(self._to_real(new_path))

    async def exists(self, path: str) -> bool:
        return self._to_real(path).exists()

    async def copy(self, src: str, dst: str) -> dict[str, Any]:
        content = await self.read(src)
        return await self.write(dst, content)

    def list_mounts(self) -> list[str]:
        return [self._mount_point]

    async def close(self) -> None:
        pass

    # -- Internal --

    def _stat_entry(self, real_path: Path) -> dict[str, Any]:
        """Build a stat dict from a real path."""
        st = real_path.stat()
        is_dir = real_path.is_dir()
        mime, _ = mimetypes.guess_type(str(real_path))
        return {
            "path": self._to_virtual(real_path),
            "size": 4096 if is_dir else st.st_size,
            "is_directory": is_dir,
            "etag": None,
            "mime_type": mime or ("inode/directory" if is_dir else "application/octet-stream"),
            "created_at": datetime.fromtimestamp(st.st_ctime, tz=UTC).isoformat(),
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
            "version": 0,
            "zone_id": "root",
            "entry_type": 1 if is_dir else 0,
        }
