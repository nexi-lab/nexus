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
from typing import TYPE_CHECKING, Any, cast

try:
    from fsspec.spec import AbstractBufferedFile, AbstractFileSystem
except ImportError:
    raise ImportError(
        "fsspec is required for NexusFileSystem. Install with: pip install nexus-fs[fsspec]"
    ) from None

if TYPE_CHECKING:
    from nexus.fs._facade import SlimNexusFS

from nexus.fs._constants import DEFAULT_MAX_FILE_SIZE

logger = logging.getLogger(__name__)

# Re-export under descriptive names for backward compatibility.
MAX_CAT_FILE_SIZE = DEFAULT_MAX_FILE_SIZE
MAX_WRITE_BUFFER_SIZE = DEFAULT_MAX_FILE_SIZE

# Warn when write buffer crosses 100 MB (before the 1 GB hard limit).
_WRITE_BUFFER_WARNING = 100 * 1024 * 1024

# Supported modes for _open().
_SUPPORTED_MODES = frozenset({"rb", "wb", "r", "w", "xb", "x"})


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

    # -- Auto-discovery --------------------------------------------------------

    @staticmethod
    def _auto_discover() -> SlimNexusFS:
        """Auto-discover mounts from ``mounts.json``.

        Reads the mount entries persisted by ``mount()`` and boots a
        SlimNexusFS facade.  This enables ``fsspec.filesystem("nexus")``
        and ``pd.read_csv("nexus:///...")`` without explicit construction.

        Raises:
            FileNotFoundError: If no ``mounts.json`` exists.
            ValueError: If ``mounts.json`` is empty or invalid.
        """
        from nexus.fs._paths import build_mount_args, load_persisted_mounts, mounts_file

        mf = mounts_file()

        if not mf.exists():
            raise FileNotFoundError(
                "No nexus-fs mounts found. Run nexus.fs.mount() or "
                "nexus.fs.mount_sync() first to register backends, "
                "then use fsspec.open('nexus:///...')."
            )

        entries = load_persisted_mounts()

        if not entries:
            raise ValueError(
                f"Invalid mounts.json at {mf}. Run nexus.fs.mount() to re-register backends."
            )

        from nexus.fs import mount
        from nexus.fs._sync import run_sync

        uris, overrides = build_mount_args(entries)
        return cast("SlimNexusFS", run_sync(mount(*uris, mount_overrides=overrides or None)))

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
            cached: list[Any] = self.dircache[path]
            if detail:
                return cached
            return [e["name"] for e in cached]

        # Verify path exists — fsspec contract requires FileNotFoundError
        stat = self._nexus.stat(path)
        if stat is None:
            raise FileNotFoundError(path)

        entries: Any = self._nexus.ls(path, detail=True, recursive=False)

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
        stat = self._nexus.stat(path)
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
            stat = self._nexus.stat(path)
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
            return self._nexus.read_range(path, range_start, range_end)

        # Full read — apply size guard
        stat = self._nexus.stat(path)
        if stat is None:
            raise FileNotFoundError(path)
        if stat.get("size", 0) > MAX_CAT_FILE_SIZE:
            size_gb = stat["size"] / (1024**3)
            raise ValueError(
                f"File too large for _cat_file ({size_gb:.1f} GB > 1 GB limit). "
                f"Use open() for streaming access."
            )

        return self._nexus.read(path)

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
        self._nexus.write(path, value)

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
        stat = self._nexus.stat(path)
        if stat and stat.get("is_directory"):
            self._nexus.rmdir(path, recursive=recursive)
        else:
            self._nexus.delete(path)

    # -- Copy ------------------------------------------------------------------

    def _cp_file(self, path1: str, path2: str, **kwargs: Any) -> None:  # noqa: ARG002
        """Copy a single file."""
        path1 = self._strip_protocol(path1)
        path2 = self._strip_protocol(path2)
        self._nexus.copy(path1, path2)

    # -- Directories -----------------------------------------------------------

    def _mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> None:  # noqa: ARG002
        """Create directory."""
        path = self._strip_protocol(path)
        self._nexus.mkdir(path, parents=create_parents)

    def mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> None:  # noqa: ARG002
        """Create directory (public API).

        Overrides the no-op default in AbstractFileSystem so that
        ``fs.mkdir()`` and ``fs.makedirs()`` actually create directories.
        """
        self._mkdir(path, create_parents=create_parents)
        self.dircache.clear()

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        """Create directory and parents (public API)."""
        path = self._strip_protocol(path)
        if not exist_ok:
            stat = self._nexus.stat(path)
            if stat is not None and stat.get("is_directory"):
                raise FileExistsError(path)
        try:
            self._mkdir(path, create_parents=True)
        except Exception as exc:
            if exist_ok:
                logger.debug(
                    "makedirs(%r, exist_ok=True) suppressed: %s",
                    path,
                    exc,
                )
            else:
                raise
        self.dircache.clear()

    def cp_file(self, path1: str, path2: str, **kwargs: Any) -> None:
        """Copy a single file (public API).

        Overrides the NotImplementedError default in AbstractFileSystem.
        Handles directories (fsspec copy(recursive=True) calls cp_file
        on each entry including directories) and ensures parent
        directories exist in the metastore for ls() discovery.
        """
        import posixpath

        path1 = self._strip_protocol(path1)
        path2 = self._strip_protocol(path2)

        if self.isdir(path1):
            self.mkdir(path2)
        else:
            parent = posixpath.dirname(path2)
            if parent and not self.isdir(parent):
                self.makedirs(parent, exist_ok=True)
            self._cp_file(path1, path2, **kwargs)
        self.dircache.clear()

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

        if "x" in mode:
            # Exclusive create — fail if file exists
            if self._nexus.stat(path) is not None:
                raise FileExistsError(path)
            return NexusWriteFile(
                fs=self,
                path=path,
                nexus_fs=self._nexus,
            )

        if "r" in mode:
            stat = self._nexus.stat(path)
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
            )
        else:
            return NexusWriteFile(
                fs=self,
                path=path,
                nexus_fs=self._nexus,
            )


class NexusBufferedFile(AbstractBufferedFile):
    """Read-only buffered file backed by nexus-fs ``read_range()``.

    Inherits from fsspec's ``AbstractBufferedFile`` to get the full
    file-like contract (read, readline, readlines, seek, tell, close,
    context manager, iteration) plus cache strategies (readahead, block,
    bytes, all).

    Only ``_fetch_range()`` is overridden — everything else is inherited.
    """

    def __init__(
        self,
        fs: NexusFileSystem,
        path: str,
        mode: str,
        size: int,
        block_size: int,
        nexus_fs: SlimNexusFS,
    ) -> None:
        self._nexus = nexus_fs
        # AbstractBufferedFile only accepts binary modes (rb, wb, ab, xb).
        # Normalize "r" → "rb" since our text mode returns bytes anyway.
        abf_mode = mode if mode.endswith("b") else mode + "b"
        super().__init__(
            fs=fs,
            path=path,
            mode=abf_mode,
            block_size=block_size,
            size=size,
            cache_type="readahead",
        )

    @property
    def name(self) -> str:
        return str(self.path)

    def _fetch_range(self, start: int, end: int) -> bytes:
        """Fetch byte range from nexus backend via ``read_range()``."""
        return bytes(self._nexus.read_range(self.path, start, end))


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
    ) -> None:
        self.fs = fs
        self.path = path
        self._nexus = nexus_fs
        self._buffer = io.BytesIO()
        self._closed = False
        self._bytes_written = 0
        self._warned_large_buffer = False

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
        if not self._warned_large_buffer and self._bytes_written > _WRITE_BUFFER_WARNING:
            self._warned_large_buffer = True
            logger.warning(
                "Write buffer for %r is %d MB. Writes are buffered in memory "
                "up to 1 GB (hard limit). Consider chunking large writes.",
                self.path,
                self._bytes_written // (1024 * 1024),
            )
        return self._buffer.write(data)

    def flush(self) -> None:
        """No-op -- data is flushed on close()."""

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._buffer.seek(0)
            self._nexus.write(self.path, self._buffer.read())

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
