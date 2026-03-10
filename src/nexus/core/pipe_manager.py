"""PipeManager — VFS named pipe manager (fs/pipe.c equivalent).

Kernel primitive (§4.2) managing DT_PIPE lifecycle and buffer registry
with per-pipe locking for MPMC.

    core/pipe.py         = kfifo     (include/linux/kfifo.h + lib/kfifo.c)
    core/pipe_manager.py = fs/pipe.c (VFS named pipe with per-pipe lock)

Concurrency model (aligned with Linux pipe(7)):
  - RingBuffer (kfifo) is SPSC, no internal lock.
  - PipeManager (mkfifo) adds per-pipe asyncio.Lock for MPMC safety.
    Async methods use Linux-style lock→try_nowait→unlock→wait→retry
    to avoid holding the lock during blocking waits (deadlock-free).
  - Sync methods (pipe_write_nowait) are atomic under asyncio event loop
    (no await points), safe for MPSC without lock.

See: core/pipe.py for RingBuffer, federation-memo.md §7j
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.pipe import (
    PipeClosedError,
    PipeEmptyError,
    PipeError,
    PipeFullError,
    PipeNotFoundError,
    RingBuffer,
)

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)

# Re-export exceptions so callers can import from either module
__all__ = [
    "PipeManager",
    "PipeError",
    "PipeFullError",
    "PipeEmptyError",
    "PipeClosedError",
    "PipeNotFoundError",
]


class PipeManager:
    """Manages DT_PIPE lifecycle and buffer registry.

    Analogous to Linux fs/pipe.c: creates named pipes visible in the VFS
    namespace. Each pipe has a FileMetadata inode in MetastoreABC
    (entry_type=DT_PIPE) and a RingBuffer in process memory.

    The inode provides:
      - VFS path (/nexus/pipes/{name}) for agent access via FUSE/MCP
      - ReBAC access control (owner_id, permission checks)
      - Observability (list all pipes, inspect stats)

    The ring buffer data is NOT in any storage pillar — it's process heap
    memory, like Linux kfifo data in kmalloc'd kernel heap.
    """

    def __init__(self, metastore: "MetastoreABC", zone_id: str = ROOT_ZONE_ID) -> None:
        self._metastore = metastore
        self._zone_id = zone_id
        self._buffers: dict[str, RingBuffer] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def create(
        self,
        path: str,
        *,
        capacity: int = 65_536,
        owner_id: str | None = None,
    ) -> RingBuffer:
        """Create a new named pipe at the given VFS path.

        Creates a DT_PIPE inode in MetastoreABC and a RingBuffer in memory.

        Args:
            path: VFS path (e.g., "/nexus/pipes/my-pipe"). Must start with "/".
            capacity: Ring buffer byte capacity. Default 64KB.
            owner_id: Owner for ReBAC permission checks.

        Returns:
            The created RingBuffer.

        Raises:
            PipeError: Pipe already exists at this path.
        """
        from nexus.contracts.metadata import DT_PIPE, FileMetadata

        if path in self._buffers:
            raise PipeError(f"pipe already exists: {path}")

        # Check if inode already exists in metastore
        existing = self._metastore.get(path)
        if existing is not None:
            raise PipeError(f"path already exists: {path}")

        # Create DT_PIPE inode in MetastoreABC
        metadata = FileMetadata(
            path=path,
            backend_name="pipe",
            physical_path="mem://",
            size=capacity,
            entry_type=DT_PIPE,
            zone_id=self._zone_id,
            owner_id=owner_id,
        )
        self._metastore.put(metadata)

        # Create in-memory ring buffer
        buf = RingBuffer(capacity=capacity)
        self._buffers[path] = buf

        logger.debug("pipe created: %s (capacity=%d)", path, capacity)
        return buf

    def open(self, path: str, *, capacity: int = 65_536) -> RingBuffer:
        """Open an existing pipe, or recover its buffer after restart.

        If the buffer is already in memory, returns it. If a DT_PIPE inode
        exists but the buffer was lost (process restart), creates a new
        buffer for the existing inode.

        Args:
            path: VFS path of the pipe.
            capacity: Buffer capacity (used only if recreating after restart).

        Returns:
            The RingBuffer for this pipe.

        Raises:
            PipeNotFoundError: No pipe inode at this path.
        """
        from nexus.contracts.metadata import DT_PIPE

        # Return existing buffer if available
        if path in self._buffers and not self._buffers[path].closed:
            return self._buffers[path]

        # Check metastore for inode
        metadata = self._metastore.get(path)
        if metadata is None or metadata.entry_type != DT_PIPE:
            raise PipeNotFoundError(f"no pipe at: {path}")

        # Recreate buffer (restart recovery)
        buf = RingBuffer(capacity=capacity)
        self._buffers[path] = buf

        logger.debug("pipe opened (recovered): %s", path)
        return buf

    def signal_close(self, path: str) -> None:
        """Signal a pipe closed without removing from registry.

        Closes the RingBuffer (wakes blocked readers/writers) but keeps
        it in ``_buffers`` so ``pipe_read()`` can drain remaining messages.
        Once the buffer is empty, readers get ``PipeClosedError``.

        Use this for graceful shutdown: signal_close → consumer drains → done.
        Use ``close()`` for immediate teardown.

        Raises:
            PipeNotFoundError: No buffer at this path.
        """
        buf = self._buffers.get(path)
        if buf is None:
            raise PipeNotFoundError(f"no pipe at: {path}")
        buf.close()
        logger.debug("pipe signal_close: %s", path)

    def close(self, path: str) -> None:
        """Close a pipe's buffer and remove from registry. Inode stays in MetastoreABC.

        Raises:
            PipeNotFoundError: No buffer at this path.
        """
        buf = self._buffers.pop(path, None)
        if buf is None:
            raise PipeNotFoundError(f"no pipe at: {path}")
        buf.close()
        self._locks.pop(path, None)
        logger.debug("pipe closed: %s", path)

    def destroy(self, path: str) -> None:
        """Close buffer AND delete inode from MetastoreABC.

        Raises:
            PipeNotFoundError: No pipe at this path.
        """
        buf = self._buffers.pop(path, None)
        if buf is not None:
            buf.close()
        self._locks.pop(path, None)

        metadata = self._metastore.get(path)
        if metadata is None:
            if buf is None:
                raise PipeNotFoundError(f"no pipe at: {path}")
            return

        self._metastore.delete(path)
        logger.debug("pipe destroyed: %s", path)

    def _get_buffer(self, path: str) -> RingBuffer:
        """Get buffer or raise PipeNotFoundError."""
        buf = self._buffers.get(path)
        if buf is None:
            raise PipeNotFoundError(f"no pipe at: {path}")
        return buf

    def _get_lock(self, path: str) -> asyncio.Lock:
        """Get or create per-pipe lock for MPMC safety."""
        lock = self._locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[path] = lock
        return lock

    # ------------------------------------------------------------------
    # Data path — MPMC-safe read/write
    # ------------------------------------------------------------------

    async def pipe_write(self, path: str, data: bytes, *, blocking: bool = True) -> int:
        """Write to a named pipe. MPMC-safe (per-pipe asyncio.Lock).

        Uses Linux pipe_write pattern: lock → try_nowait → unlock → wait → retry.
        Lock is never held during blocking waits (deadlock-free).
        """
        buf = self._get_buffer(path)
        lock = self._get_lock(path)
        while True:
            async with lock:
                try:
                    return buf.write_nowait(data)
                except PipeFullError:
                    if not blocking:
                        raise
            # Full and blocking: wait for space without holding lock
            await buf.wait_writable()

    async def pipe_read(self, path: str, *, blocking: bool = True) -> bytes:
        """Read from a named pipe. MPMC-safe (per-pipe asyncio.Lock).

        Uses Linux pipe_read pattern: lock → try_nowait → unlock → wait → retry.
        Lock is never held during blocking waits (deadlock-free).
        """
        buf = self._get_buffer(path)
        lock = self._get_lock(path)
        while True:
            async with lock:
                try:
                    return buf.read_nowait()
                except PipeEmptyError:
                    if not blocking:
                        raise
            # Empty and blocking: wait for data without holding lock
            await buf.wait_readable()

    def pipe_write_nowait(self, path: str, data: bytes) -> int:
        """Synchronous non-blocking write to a named pipe.

        Atomic under asyncio event loop (no await points = no preemption).
        Safe for MPSC without lock. Used by sync producers (e.g. VFS write/delete/rename).
        """
        return self._get_buffer(path).write_nowait(data)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def pipe_peek(self, path: str) -> bytes | None:
        """Peek at next message in a named pipe."""
        return self._get_buffer(path).peek()

    def pipe_peek_all(self, path: str) -> list[bytes]:
        """Peek at all messages in a named pipe."""
        return self._get_buffer(path).peek_all()

    def list_pipes(self) -> dict[str, dict]:
        """List all active pipes with their stats."""
        return {path: buf.stats for path, buf in self._buffers.items()}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close_all(self) -> None:
        """Close all pipe buffers. Called on kernel shutdown."""
        for path, buf in self._buffers.items():
            buf.close()
            logger.debug("pipe closed (shutdown): %s", path)
        self._buffers.clear()
        self._locks.clear()
