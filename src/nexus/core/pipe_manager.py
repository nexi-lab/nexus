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
    PipeBackend,
    PipeClosedError,
    PipeEmptyError,
    PipeError,
    PipeFullError,
    PipeNotFoundError,
    RingBuffer,
)

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.grpc.channel_pool import PeerChannelPool

logger = logging.getLogger(__name__)

# Re-export exceptions so callers can import from either module
__all__ = [
    "PipeManager",
    "PipeBackend",
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

    def __init__(
        self,
        metastore: "MetastoreABC",
        self_address: str | None = None,
        channel_pool: "PeerChannelPool | None" = None,
    ) -> None:
        self._metastore = metastore
        self._self_address = self_address
        self._channel_pool = channel_pool
        self._buffers: dict[str, PipeBackend] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def self_address(self) -> str | None:
        """This node's advertise address, or None for single-node mode."""
        return self._self_address

    def create(
        self,
        path: str,
        *,
        capacity: int = 65_536,
        owner_id: str | None = None,
        zone_id: str = ROOT_ZONE_ID,
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

        # Create DT_PIPE inode in MetastoreABC.
        # Embed origin address so remote nodes can proxy pipe I/O.
        # "pipe" (no origin) = single-node mode, "pipe@host:port" = federated.
        pipe_backend = f"pipe@{self._self_address}" if self._self_address else "pipe"
        metadata = FileMetadata(
            path=path,
            backend_name=pipe_backend,
            physical_path="mem://",
            size=capacity,
            entry_type=DT_PIPE,
            zone_id=zone_id,
            owner_id=owner_id,
        )
        self._metastore.put(metadata)

        # Create in-memory ring buffer
        buf = RingBuffer(capacity=capacity)
        self._buffers[path] = buf

        logger.debug("pipe created: %s (capacity=%d)", path, capacity)
        return buf

    def ensure(
        self,
        path: str,
        *,
        capacity: int = 65_536,
        owner_id: str | None = None,
        zone_id: str = ROOT_ZONE_ID,
    ) -> PipeBackend:
        """Ensure a named pipe has both an inode and a live in-memory buffer.

        This is the idempotent startup path for long-lived DT_PIPE services.
        It handles three cases:

        1. pipe already open in-memory -> return existing buffer
        2. no inode yet -> create inode + buffer
        3. inode persisted but buffer lost after restart -> reopen buffer
        """
        if path in self._buffers and not self._buffers[path].closed:
            return self._buffers[path]

        try:
            return self.create(
                path,
                capacity=capacity,
                owner_id=owner_id,
                zone_id=zone_id,
            )
        except PipeError:
            return self.open(path, capacity=capacity)

    def open(self, path: str, *, capacity: int = 65_536) -> PipeBackend:
        """Open an existing pipe, or recover its buffer after restart.

        If the buffer is already in memory, returns it. If a DT_PIPE inode
        exists but the buffer was lost (process restart), creates a new
        buffer for the existing inode.

        For remote pipes (origin != self_address), installs a
        RemotePipeBackend that proxies via persistent gRPC channel.

        Args:
            path: VFS path of the pipe.
            capacity: Buffer capacity (used only if recreating after restart).

        Returns:
            The PipeBackend for this pipe (RingBuffer or RemotePipeBackend).

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

        # Detect remote pipe — install RemotePipeBackend for fast-path
        if self._channel_pool is not None and metadata.backend_name:
            from nexus.contracts.backend_address import BackendAddress

            addr = BackendAddress.parse(metadata.backend_name)
            if addr.has_origin and self._self_address not in addr.origins:
                from nexus.core.remote_pipe import RemotePipeBackend

                backend: PipeBackend = RemotePipeBackend(
                    origin=addr.origins[0],
                    path=path,
                    channel_pool=self._channel_pool,
                )
                self._buffers[path] = backend
                logger.debug("pipe opened (remote): %s → %s", path, addr.origins[0])
                return backend

        # Local: recreate buffer (restart recovery)
        buf = RingBuffer(capacity=capacity)
        self._buffers[path] = buf

        logger.debug("pipe opened (recovered): %s", path)
        return buf

    def create_from_backend(
        self,
        path: str,
        backend: PipeBackend,
        *,
        owner_id: str | None = None,
        zone_id: str = ROOT_ZONE_ID,
    ) -> PipeBackend:
        """Create a named pipe backed by an external PipeBackend.

        Unlike ``create()`` which always uses an in-process RingBuffer,
        this method accepts any PipeBackend implementation (e.g., a future
        SharedMemoryPipeBackend for inter-process IPC).

        The DT_PIPE inode is still registered in MetastoreABC for VFS
        visibility and ReBAC.

        Args:
            path: VFS path. Must start with "/".
            backend: The PipeBackend instance to use for data transport.
            owner_id: Owner for ReBAC permission checks.

        Returns:
            The registered PipeBackend (same object passed in).

        Raises:
            PipeError: Pipe already exists at this path.
        """
        from nexus.contracts.metadata import DT_PIPE, FileMetadata

        if path in self._buffers:
            raise PipeError(f"pipe already exists: {path}")

        existing = self._metastore.get(path)
        if existing is not None:
            raise PipeError(f"path already exists: {path}")

        pipe_backend_name = f"pipe@{self._self_address}" if self._self_address else "pipe"
        metadata = FileMetadata(
            path=path,
            backend_name=pipe_backend_name,
            physical_path="mem://",
            size=0,
            entry_type=DT_PIPE,
            zone_id=zone_id,
            owner_id=owner_id,
        )
        self._metastore.put(metadata)

        self._buffers[path] = backend
        logger.debug("pipe created (custom backend): %s", path)
        return backend

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

    def _get_buffer(self, path: str) -> PipeBackend:
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
        """Peek at next message in a named pipe.

        Only supported for RingBuffer backends. Returns None for other backends.
        """
        buf = self._get_buffer(path)
        if isinstance(buf, RingBuffer):
            return buf.peek()
        return None

    def pipe_peek_all(self, path: str) -> list[bytes]:
        """Peek at all messages in a named pipe.

        Only supported for RingBuffer backends. Returns empty list for others.
        """
        buf = self._get_buffer(path)
        if isinstance(buf, RingBuffer):
            return buf.peek_all()
        return []

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
