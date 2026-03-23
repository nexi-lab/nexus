"""fsspec compatibility layer for nexus-fs.

Implements ``fsspec.AbstractFileSystem`` so that nexus mounts are accessible
via standard Python data tools:

    pd.read_csv("nexus:///s3/my-bucket/data.csv")

Registration:
    # Via entry point (pyproject.toml):
    [project.entry-points."fsspec.specs"]
    nexus = "nexus.fs._fsspec:NexusFileSystem"

    # Or via code:
    from fsspec import register_implementation
    from nexus.fs._fsspec import NexusFileSystem
    register_implementation("nexus", NexusFileSystem)
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.fs._facade import SlimNexusFS

logger = logging.getLogger(__name__)

# Maximum file size for _cat_file before refusing (1 GB).
# Files larger than this should use _open() with streaming.
MAX_CAT_FILE_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB


def _get_fs_class() -> tuple[type, type]:
    """Lazy-import fsspec base classes to avoid hard dependency."""
    try:
        from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

        return AbstractFileSystem, AbstractBufferedFile
    except ImportError:
        raise ImportError(
            "fsspec is required for NexusFileSystem. Install with: pip install nexus-fs[fsspec]"
        ) from None


class NexusFileSystem:
    """fsspec-compatible filesystem backed by nexus-fs mounts.

    This class is constructed lazily — it inherits from AbstractFileSystem
    only when fsspec is available. Use ``NexusFileSystem.create()`` or
    import directly (which triggers the fsspec import check).

    Parameters:
        nexus_fs: A SlimNexusFS facade instance.
    """

    protocol = ("nexus",)

    def __init__(self, nexus_fs: SlimNexusFS, **kwargs: Any) -> None:
        AbstractFileSystem, _ = _get_fs_class()
        # Mixin the base class dynamically
        if not isinstance(self, AbstractFileSystem):
            self.__class__ = type(
                "NexusFileSystem",
                (NexusFileSystem, AbstractFileSystem),
                {},
            )
            super().__init__(**kwargs)
        self._nexus = nexus_fs
        self._sync = _get_sync_caller()

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        """Remove nexus:// protocol prefix from path."""
        if path.startswith("nexus://"):
            path = path[len("nexus://") :]
        if path.startswith("nexus:"):
            path = path[len("nexus:") :]
        # Ensure leading slash
        if not path.startswith("/"):
            path = "/" + path
        return path

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list:  # noqa: ARG002
        """List objects at path.

        Args:
            path: Directory path to list.
            detail: If True, return list of dicts. If False, return list of paths.

        Returns:
            List of entries (dicts with name/size/type if detail=True).
        """
        path = self._strip_protocol(path)
        entries = self._sync(self._nexus.ls(path, detail=True, recursive=False))

        if detail:
            return [
                {
                    "name": e["path"],
                    "size": e.get("size", 0),
                    "type": "directory" if e.get("entry_type", 0) == 1 else "file",
                }
                for e in entries
            ]
        return [e["path"] for e in entries]

    def info(self, path: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
        """Return metadata for a single path."""
        path = self._strip_protocol(path)
        stat = self._sync(self._nexus.stat(path))
        if stat is None:
            raise FileNotFoundError(path)
        return {
            "name": stat["path"],
            "size": stat.get("size", 0),
            "type": "directory" if stat.get("is_directory") else "file",
            "etag": stat.get("etag"),
            "created": stat.get("created_at"),
            "modified": stat.get("modified_at"),
        }

    def _cat_file(
        self,
        path: str,
        start: int | None = None,
        end: int | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> bytes:
        """Read file contents with optional byte range.

        Raises ValueError if file exceeds MAX_CAT_FILE_SIZE (1 GB).
        Use _open() for large files.
        """
        path = self._strip_protocol(path)

        # Size guard: refuse to load files > 1 GB into memory
        stat = self._sync(self._nexus.stat(path))
        if stat and stat.get("size", 0) > MAX_CAT_FILE_SIZE:
            size_gb = stat["size"] / (1024**3)
            raise ValueError(
                f"File too large for _cat_file ({size_gb:.1f} GB > 1 GB limit). "
                f"Use open() for streaming access."
            )

        content: bytes = self._sync(self._nexus.read(path))

        if start is not None or end is not None:
            content = content[start:end]
        return content

    def _pipe_file(
        self,
        path: str,
        value: bytes,
        mode: str = "overwrite",  # noqa: ARG002 — fsspec API contract
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Write data to a file."""
        path = self._strip_protocol(path)
        self._sync(self._nexus.write(path, value))

    def _rm(self, path: str) -> None:
        """Delete a file."""
        path = self._strip_protocol(path)
        self._sync(self._nexus.delete(path))

    def _cp_file(self, path1: str, path2: str, **kwargs: Any) -> None:  # noqa: ARG002
        """Copy a single file."""
        path1 = self._strip_protocol(path1)
        path2 = self._strip_protocol(path2)
        self._sync(self._nexus.copy(path1, path2))

    def _mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> None:  # noqa: ARG002
        """Create directory."""
        path = self._strip_protocol(path)
        self._sync(self._nexus.mkdir(path, parents=create_parents))

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> Any:
        """Return a file-like object for streaming access.

        For read mode, returns a NexusBufferedFile that fetches byte ranges
        on demand. For write mode, returns a BytesIO that flushes to nexus
        on close.
        """
        path = self._strip_protocol(path)

        if "r" in mode:
            stat = self._sync(self._nexus.stat(path))
            if stat is None:
                raise FileNotFoundError(path)
            size = stat.get("size", 0)
            return NexusBufferedFile(
                fs=self,
                path=path,
                mode=mode,
                size=size,
                block_size=block_size or 5 * 1024 * 1024,  # 5 MB default
                nexus_fs=self._nexus,
                sync_caller=self._sync,
            )
        else:
            return NexusWriteFile(
                fs=self,
                path=path,
                nexus_fs=self._nexus,
                sync_caller=self._sync,
            )


class NexusBufferedFile:
    """Read-only file-like object with byte-range fetching.

    Implements the minimal interface needed by pandas, dask, etc.:
    read(), seek(), tell(), close(), __enter__/__exit__.
    """

    def __init__(
        self,
        fs: NexusFileSystem,
        path: str,
        mode: str,
        size: int,
        block_size: int,
        nexus_fs: SlimNexusFS,
        sync_caller: Any,
    ) -> None:
        self.fs = fs
        self.path = path
        self.mode = mode
        self.size = size
        self.block_size = block_size
        self._nexus = nexus_fs
        self._sync = sync_caller
        self._pos = 0
        self._closed = False

    def read(self, length: int = -1) -> bytes:
        """Read up to length bytes from current position.

        Uses read_range() for memory-efficient byte-range fetching — only
        the requested range is transferred from the backend, not the full file.
        """
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if self._pos >= self.size:
            return b""

        end = self.size if length == -1 else min(self._pos + length, self.size)

        # Fetch only the requested byte range — NOT the full file
        data: bytes = self._sync(self._nexus.read_range(self.path, self._pos, end))
        self._pos = end
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        """Seek to a position."""
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self.size + offset
        self._pos = max(0, min(self._pos, self.size))
        return self._pos

    def tell(self) -> int:
        return self._pos

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return True

    def __enter__(self) -> NexusBufferedFile:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class NexusWriteFile:
    """Write-only file-like object that buffers content in memory.

    Flushes to nexus on close().
    """

    def __init__(
        self,
        fs: NexusFileSystem,
        path: str,
        nexus_fs: SlimNexusFS,
        sync_caller: Any,
    ) -> None:
        self.fs = fs
        self.path = path
        self._nexus = nexus_fs
        self._sync = sync_caller
        self._buffer = io.BytesIO()
        self._closed = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed file")
        return self._buffer.write(data)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._buffer.seek(0)
            self._sync(self._nexus.write(self.path, self._buffer.read()))

    @property
    def closed(self) -> bool:
        return self._closed

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def __enter__(self) -> NexusWriteFile:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


SyncCaller = Callable[[Coroutine[Any, Any, Any]], Any]


def _get_sync_caller() -> SyncCaller:
    """Get a sync caller that runs async code synchronously.

    Uses anyio if available, falls back to asyncio.run().
    """
    try:
        from anyio.from_thread import BlockingPortalProvider

        _portal = BlockingPortalProvider()

        def sync_call(coro: Coroutine[Any, Any, Any]) -> Any:
            with _portal as portal:
                return portal.call(lambda: coro)

        return sync_call
    except ImportError:
        import asyncio

        def sync_call(coro: Coroutine[Any, Any, Any]) -> Any:
            try:
                asyncio.get_running_loop()
                # If there's a running loop, we can't use asyncio.run()
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, coro)
                    return future.result()
            except RuntimeError:
                return asyncio.run(coro)

        return sync_call
