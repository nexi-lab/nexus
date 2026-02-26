"""PipeResolver — VFSPathResolver for DT_PIPE paths (#1201).

PRE-DISPATCH resolver that short-circuits pipe I/O before PathRouter
runs.  Registered at boot via factory into KernelDispatch.

    nx.read("/pipes/agent-b/inbox")
      → KernelDispatch.resolve_read()
      → PipeResolver.matches() → True  (O(1) dict lookup)
      → PipeResolver.read()   → ring buffer → bytes
      → PathRouter NEVER called

Linux analogue: ``fifo_fops`` (fs/pipe.c) — when VFS open() hits a
FIFO inode, the kernel dispatches to pipe-specific file_operations
instead of the regular block-layer path.

See: system_services/pipe_manager.py, contracts/vfs_hooks.py §VFSPathResolver.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.vfs_hooks import VFSPathResolver
from nexus.core.pipe import PipeClosedError, PipeEmptyError, PipeNotFoundError

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.system_services.pipe_manager import PipeManager

logger = logging.getLogger(__name__)


class PipeResolver(VFSPathResolver):
    """PRE-DISPATCH resolver for DT_PIPE virtual paths.

    Implements ``VFSPathResolver`` protocol:
    - ``matches(path)`` — O(1) dict lookup + metastore fallback
    - ``read(path, ...)``  — sync non-blocking ring buffer read
    - ``write(path, content)`` — sync non-blocking ring buffer write
    - ``delete(path, ...)`` — destroy pipe (buffer + inode)

    Dependencies injected via constructor (no kernel imports at runtime):
    - pipe_manager: PipeManager (buffer registry + lifecycle)
    - metastore: MetastoreABC (inode lookup for restart recovery)
    """

    __slots__ = ("_pipe_manager", "_metastore")

    def __init__(self, pipe_manager: PipeManager, metastore: MetastoreABC) -> None:
        self._pipe_manager = pipe_manager
        self._metastore = metastore

    # ------------------------------------------------------------------
    # VFSPathResolver protocol
    # ------------------------------------------------------------------

    def matches(self, path: str) -> bool:
        """Return True if *path* is a DT_PIPE.

        Two-tier lookup:
        1. O(1) dict lookup in pipe_manager._buffers (~50ns) — catches
           all active pipes with zero metastore cost for non-pipe paths.
        2. Metastore fallback (~5μs) — catches pipes whose buffer was
           lost on restart but whose inode persists.
        """
        if path in self._pipe_manager._buffers:
            return True
        meta = self._metastore.get(path)
        return meta is not None and meta.is_pipe

    def read(
        self, path: str, *, return_metadata: bool = False, context: Any = None
    ) -> bytes | dict[str, Any]:
        """Read next message from pipe (non-blocking).

        Empty pipe returns ``b""`` (not an error) — callers needing
        blocking semantics use ``PipeManager.pipe_read()`` directly.
        """
        try:
            buf = self._pipe_manager._get_buffer(path)
        except PipeNotFoundError:
            # Try restart recovery via open()
            try:
                buf = self._pipe_manager.open(path)
                logger.debug("pipe recovered from metastore: %s (context=%s)", path, context)
            except PipeNotFoundError:
                raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None

        try:
            data = buf.read_nowait()
        except PipeEmptyError:
            data = b""
        except PipeClosedError:
            raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None

        if return_metadata:
            stats = buf.stats
            return {
                "content": data,
                "etag": f"pipe-{stats['msg_count']}",
                "version": stats["msg_count"],
                "modified_at": None,
                "size": len(data),
                "pipe_stats": stats,
            }
        return data

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        """Write message to pipe (non-blocking).

        Raises ``PipeFullError`` if the ring buffer is at capacity —
        callers needing blocking semantics use
        ``PipeManager.pipe_write()`` directly.
        """
        try:
            written = self._pipe_manager.pipe_write_nowait(path, content)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
        except PipeClosedError:
            raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None
        # PipeFullError propagates — caller decides retry/backoff

        buf = self._pipe_manager._get_buffer(path)
        stats = buf.stats
        return {
            "etag": f"pipe-{stats['msg_count']}",
            "version": stats["msg_count"],
            "size": written,
        }

    def delete(self, path: str, *, context: Any = None) -> None:
        """Destroy pipe — close buffer AND delete inode."""
        try:
            self._pipe_manager.destroy(path)
            logger.debug("pipe destroyed: %s (context=%s)", path, context)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
