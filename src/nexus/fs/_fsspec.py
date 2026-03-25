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

Validated integrations (v0.1.0):
    - ``fsspec.filesystem("nexus")`` auto-discovery via ``mounts.json``
    - ``fsspec.open("nexus:///path", "rb"/"wb")``
    - ``pd.read_csv("nexus:///path")`` (via fsspec)
    - Byte-range reads via ``read_range()``
    - ``readline()`` / ``readlines()`` / line iteration

Known deviations from full fsspec contract (v0.1.0):
    - Append mode (``"a"``/``"ab"``) is not supported — raises ``ValueError``.
    - Read-write mode (``"r+"``) is not supported — raises ``ValueError``.
    - Text mode (``"r"``/``"w"``) returns/accepts bytes, not str.
      Use ``"rb"``/``"wb"`` explicitly for clarity.
    - ``_pipe_file(mode="create")`` does not enforce create-only semantics;
      it always overwrites.
    - Write-mode ``_open()`` buffers the entire file in memory (up to 1 GB).
      True streaming writes are not yet supported.
    - ``_rm()`` does not support glob patterns; pass explicit paths.
    - ``NexusBufferedFile`` does not inherit from
      ``fsspec.spec.AbstractBufferedFile`` — it implements the file-like
      protocol directly.
    - No ``transactions`` support (``start_transaction`` / ``end_transaction``).

Framework adapters (v0.1.0 scope):
    - **fsspec**: In scope, validated with integration tests.
    - **Claude Agent SDK / Codex**: Supported via the full ``nexus`` package
      (MCP server / ACP protocol), not the slim ``nexus-fs`` package.
    - **LangChain / CrewAI / OpenAI Agents**: Available as examples in the
      full ``nexus`` repo, not shipped in the slim package.
"""

from __future__ import annotations

import io
import logging
import os
from typing import TYPE_CHECKING, Any

try:
    from fsspec.spec import AbstractFileSystem
except ImportError:
    raise ImportError(
        "fsspec is required for NexusFileSystem. Install with: pip install nexus-fs[fsspec]"
    ) from None

if TYPE_CHECKING:
    from nexus.fs._facade import SlimNexusFS
    from nexus.fs._sync import PortalRunner

logger = logging.getLogger(__name__)

# Maximum file size for _cat_file before refusing (1 GB).
# Files larger than this should use _open() with buffered access.
MAX_CAT_FILE_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB

# Maximum buffer size for write-mode _open() (1 GB).
# Writes exceeding this should use _pipe_file() with pre-built bytes
# or wait for streaming write support.
MAX_WRITE_BUFFER_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB

# Supported modes for _open().
_SUPPORTED_MODES = frozenset({"rb", "wb", "r", "w"})


class NexusFileSystem(AbstractFileSystem):
    """fsspec-compatible filesystem backed by nexus-fs mounts.

    Supports two usage patterns:

    1. **Explicit:** pass a SlimNexusFS instance directly::

           fs = NexusFileSystem(nexus_fs=my_facade)

    2. **Auto-discovery:** omit *nexus_fs* and the filesystem will
       auto-discover mounts from ``mounts.json`` (written by
       ``nexus.fs.mount()`` / ``nexus.fs.mount_sync()``)::

           pd.read_csv("nexus:///s3/my-bucket/data.csv")

    Parameters:
        nexus_fs: Optional SlimNexusFS facade instance.  When *None*,
            mounts are auto-discovered from the state directory.
    """

    protocol = ("nexus",)

    def __init__(self, nexus_fs: SlimNexusFS | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if nexus_fs is not None:
            self._nexus = nexus_fs
        else:
            self._nexus = self._auto_discover()
        from nexus.fs._sync import PortalRunner

        self._runner: PortalRunner = PortalRunner()

    # -- Auto-discovery --------------------------------------------------------

    @staticmethod
    def _auto_discover() -> SlimNexusFS:
        """Auto-discover mounts from ``mounts.json``.

        Reads the mount URIs persisted by ``mount()`` and boots a
        SlimNexusFS facade.  This enables ``fsspec.filesystem("nexus")``
        and ``pd.read_csv("nexus:///...")`` without explicit construction.

        Raises:
            FileNotFoundError: If no ``mounts.json`` exists.
            ValueError: If ``mounts.json`` is empty or invalid.
        """
        import json
        import tempfile

        state_dir = os.environ.get("NEXUS_FS_STATE_DIR") or os.path.join(
            tempfile.gettempdir(), "nexus-fs"
        )
        mounts_file = os.path.join(state_dir, "mounts.json")

        if not os.path.exists(mounts_file):
            raise FileNotFoundError(
                "No nexus-fs mounts found. Run nexus.fs.mount() or "
                "nexus.fs.mount_sync() first to register backends, "
                "then use fsspec.open('nexus:///...')."
            )

        with open(mounts_file) as f:
            uris = json.load(f)

        if not uris or not isinstance(uris, list):
            raise ValueError(
                f"Invalid mounts.json at {mounts_file}. "
                "Run nexus.fs.mount() to re-register backends."
            )

        from nexus.fs import mount
        from nexus.fs._sync import run_sync

        return run_sync(mount(*uris))

    # -- Protocol handling -----------------------------------------------------

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        """Remove ``nexus://`` protocol prefix from path."""
        if path.startswith("nexus://"):
            path = path[len("nexus://") :]
        if path.startswith("nexus:"):
            path = path[len("nexus:") :]
        # Ensure leading slash
        if not path.startswith("/"):
            path = "/" + path
        return path

    # -- Directory listing -----------------------------------------------------

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list:  # noqa: ARG002
        """List objects at path.

        Args:
            path: Directory path to list.
            detail: If True, return list of dicts.  If False, return
                list of paths.

        Returns:
            List of entries (dicts with name/size/type if detail=True).

        Raises:
            FileNotFoundError: If path does not exist.
        """
        path = self._strip_protocol(path)

        # Check dircache first
        if path in self.dircache:
            cached = self.dircache[path]
            if detail:
                return cached
            return [e["name"] for e in cached]

        # Verify path exists — fsspec contract requires FileNotFoundError
        stat = self._runner(self._nexus.stat(path))
        if stat is None:
            raise FileNotFoundError(path)

        entries = self._runner(self._nexus.ls(path, detail=True, recursive=False))

        result = [
            {
                "name": e["path"],
                "size": e.get("size", 0),
                "type": "directory" if e.get("entry_type", 0) == 1 else "file",
            }
            for e in entries
        ]

        # Populate fsspec dircache so subsequent info() calls hit cache.
        self.dircache[path] = result

        if detail:
            return result
        return [e["name"] for e in result]

    # -- Metadata --------------------------------------------------------------

    def info(self, path: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
        """Return metadata for a single path."""
        path = self._strip_protocol(path)
        stat = self._runner(self._nexus.stat(path))
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

    # -- Read ------------------------------------------------------------------

    def _cat_file(
        self,
        path: str,
        start: int | None = None,
        end: int | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> bytes:
        """Read file contents with optional byte range.

        When *start*/*end* are provided, uses ``read_range()`` to fetch
        only the requested bytes from the backend.  Otherwise reads the
        full file.

        Raises ValueError if file exceeds ``MAX_CAT_FILE_SIZE`` (1 GB)
        for full reads.  Use ``_open()`` for large files.
        """
        path = self._strip_protocol(path)

        if start is not None or end is not None:
            # Byte-range read — resolve negative indices, delegate to read_range
            stat = self._runner(self._nexus.stat(path))
            if stat is None:
                raise FileNotFoundError(path)
            file_size = stat.get("size", 0)
            range_start = start if start is not None else 0
            range_end = end if end is not None else file_size
            # Handle negative indices (Python slice semantics)
            if range_start < 0:
                range_start = max(0, file_size + range_start)
            if range_end < 0:
                range_end = max(0, file_size + range_end)
            return self._runner(self._nexus.read_range(path, range_start, range_end))

        # Full read — apply size guard
        stat = self._runner(self._nexus.stat(path))
        if stat is None:
            raise FileNotFoundError(path)
        if stat.get("size", 0) > MAX_CAT_FILE_SIZE:
            size_gb = stat["size"] / (1024**3)
            raise ValueError(
                f"File too large for _cat_file ({size_gb:.1f} GB > 1 GB limit). "
                f"Use open() for streaming access."
            )

        return self._runner(self._nexus.read(path))

    # -- Write -----------------------------------------------------------------

    def _pipe_file(
        self,
        path: str,
        value: bytes,
        mode: str = "overwrite",  # noqa: ARG002 -- fsspec API contract
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Write data to a file."""
        path = self._strip_protocol(path)
        self._runner(self._nexus.write(path, value))

    # -- Delete ----------------------------------------------------------------

    def _rm(self, path: str, recursive: bool = False, **kwargs: Any) -> None:  # noqa: ARG002
        """Delete a file or directory.

        Args:
            path: Path to delete.
            recursive: If True and path is a directory, remove
                recursively.
        """
        path = self._strip_protocol(path)
        # Check if it's a directory — if so, use rmdir
        stat = self._runner(self._nexus.stat(path))
        if stat and stat.get("is_directory"):
            self._runner(self._nexus.rmdir(path, recursive=recursive))
        else:
            self._runner(self._nexus.delete(path))

    # -- Copy ------------------------------------------------------------------

    def _cp_file(self, path1: str, path2: str, **kwargs: Any) -> None:  # noqa: ARG002
        """Copy a single file."""
        path1 = self._strip_protocol(path1)
        path2 = self._strip_protocol(path2)
        self._runner(self._nexus.copy(path1, path2))

    # -- Directories -----------------------------------------------------------

    def _mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> None:  # noqa: ARG002
        """Create directory."""
        path = self._strip_protocol(path)
        self._runner(self._nexus.mkdir(path, parents=create_parents))

    # -- Open ------------------------------------------------------------------

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> Any:
        """Return a file-like object for buffered access.

        For read mode (``'rb'``/``'r'``), returns a ``NexusBufferedFile``
        that fetches byte ranges on demand.  For write mode
        (``'wb'``/``'w'``), returns a ``NexusWriteFile`` that buffers
        content in memory and flushes on close.

        Note: write mode buffers the entire file in memory (up to 1 GB).
        For larger writes, use ``_pipe_file()`` with pre-built bytes.

        Supported modes: ``'rb'``, ``'wb'``, ``'r'``, ``'w'``.
        """
        if mode not in _SUPPORTED_MODES:
            raise ValueError(
                f"Unsupported mode {mode!r}. "
                f"Supported modes: {', '.join(sorted(_SUPPORTED_MODES))}. "
                f"Append ('a'/'ab') and read-write ('r+') modes are not supported."
            )

        path = self._strip_protocol(path)

        if "r" in mode:
            stat = self._runner(self._nexus.stat(path))
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
                runner=self._runner,
            )
        else:
            return NexusWriteFile(
                fs=self,
                path=path,
                nexus_fs=self._nexus,
                runner=self._runner,
            )


class NexusBufferedFile:
    """Read-only file-like object with byte-range fetching.

    Implements the file-like interface needed by pandas, dask, etc.:
    ``read()``, ``readline()``, ``readlines()``, ``seek()``, ``tell()``,
    ``close()``, ``__enter__``/``__exit__``, ``__iter__``/``__next__``.
    """

    def __init__(
        self,
        fs: NexusFileSystem,
        path: str,
        mode: str,
        size: int,
        block_size: int,
        nexus_fs: SlimNexusFS,
        runner: PortalRunner,
    ) -> None:
        self.fs = fs
        self.path = path
        self.mode = mode
        self.size = size
        self.block_size = block_size
        self._nexus = nexus_fs
        self._runner = runner
        self._pos = 0
        self._closed = False

    @property
    def name(self) -> str:
        return self.path

    def read(self, length: int = -1) -> bytes:
        """Read up to *length* bytes from current position.

        Uses ``read_range()`` for memory-efficient byte-range fetching --
        only the requested range is transferred from the backend.
        """
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if self._pos >= self.size:
            return b""

        end = self.size if length == -1 else min(self._pos + length, self.size)

        data: bytes = self._runner(self._nexus.read_range(self.path, self._pos, end))
        self._pos = end
        return data

    def readline(self, size: int = -1) -> bytes:
        r"""Read a single line (up to ``\n`` or EOF).

        Args:
            size: Maximum bytes to read.  -1 means no limit.
        """
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if self._pos >= self.size:
            return b""

        chunks: list[bytes] = []
        bytes_read = 0
        chunk_size = min(self.block_size, 8192)

        while self._pos < self.size:
            if 0 <= size <= bytes_read:
                break
            remaining = self.size - self._pos
            if size >= 0:
                remaining = min(remaining, size - bytes_read)
            to_read = min(chunk_size, remaining)

            chunk: bytes = self._runner(
                self._nexus.read_range(self.path, self._pos, self._pos + to_read)
            )
            if not chunk:
                break

            newline_pos = chunk.find(b"\n")
            if newline_pos >= 0:
                # Found newline -- take up to and including it
                chunks.append(chunk[: newline_pos + 1])
                self._pos += newline_pos + 1
                break
            else:
                chunks.append(chunk)
                self._pos += len(chunk)
                bytes_read += len(chunk)

        return b"".join(chunks)

    def readlines(self, hint: int = -1) -> list[bytes]:
        """Read all remaining lines.

        Args:
            hint: Approximate number of bytes to read.  -1 reads all.
        """
        lines: list[bytes] = []
        total = 0
        while True:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if 0 <= hint <= total:
                break
        return lines

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

    def flush(self) -> None:
        """No-op for read-only file."""

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

    def __iter__(self) -> NexusBufferedFile:
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line


class NexusWriteFile:
    """Write-only file-like object that buffers content in memory.

    Buffers all written data in memory and flushes to nexus on
    ``close()``.  Maximum buffer size is ``MAX_WRITE_BUFFER_SIZE``
    (1 GB) -- writes exceeding this limit will raise ``ValueError``.

    For larger files, use ``_pipe_file()`` with pre-built bytes.
    """

    def __init__(
        self,
        fs: NexusFileSystem,
        path: str,
        nexus_fs: SlimNexusFS,
        runner: PortalRunner,
    ) -> None:
        self.fs = fs
        self.path = path
        self._nexus = nexus_fs
        self._runner = runner
        self._buffer = io.BytesIO()
        self._closed = False
        self._bytes_written = 0

    @property
    def name(self) -> str:
        return self.path

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed file")
        self._bytes_written += len(data)
        if self._bytes_written > MAX_WRITE_BUFFER_SIZE:
            raise ValueError(
                f"Write buffer exceeded "
                f"{MAX_WRITE_BUFFER_SIZE / (1024**3):.0f} GB limit. "
                f"Use _pipe_file() for large writes or wait for "
                f"streaming write support."
            )
        return self._buffer.write(data)

    def flush(self) -> None:
        """No-op -- data is flushed on close()."""

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._buffer.seek(0)
            self._runner(self._nexus.write(self.path, self._buffer.read()))

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
