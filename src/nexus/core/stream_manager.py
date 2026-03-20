"""StreamManager — VFS named stream manager (mkstream equivalent).

Kernel primitive (§4.2) managing DT_STREAM lifecycle and buffer registry
with per-stream locking for concurrent writers.

    core/stream.py         = kstream  (linear append-only buffer)
    core/stream_manager.py = mkstream (VFS named stream with per-stream lock)

Concurrency model:
  - StreamBuffer (kstream) is single-writer internally (linear append).
  - StreamManager (mkstream) adds per-stream asyncio.Lock for concurrent
    writers. Reads are lock-free (non-destructive, offset-based).

See: core/stream.py for StreamBuffer, federation-memo.md §7j
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.stream import (
    StreamBackend,
    StreamBuffer,
    StreamClosedError,
    StreamEmptyError,
    StreamError,
    StreamFullError,
    StreamNotFoundError,
)

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)

# Re-export exceptions so callers can import from either module
__all__ = [
    "StreamManager",
    "StreamBackend",
    "StreamError",
    "StreamFullError",
    "StreamEmptyError",
    "StreamClosedError",
    "StreamNotFoundError",
]


class StreamManager:
    """Manages DT_STREAM lifecycle and buffer registry.

    Analogous to PipeManager but for append-only streams. Each stream has
    a FileMetadata inode in MetastoreABC (entry_type=DT_STREAM) and a
    StreamBuffer in process memory.

    Key difference from PipeManager: reads are non-destructive and lock-free.
    Multiple readers maintain independent byte offsets (fan-out). Writers
    use a per-stream lock for MPMC safety.
    """

    def __init__(
        self,
        metastore: "MetastoreABC",
        self_address: str | None = None,
    ) -> None:
        self._metastore = metastore
        self._self_address = self_address
        self._buffers: dict[str, StreamBackend] = {}
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
    ) -> StreamBuffer:
        """Create a new named stream at the given VFS path.

        Creates a DT_STREAM inode in MetastoreABC and a StreamBuffer in memory.

        Args:
            path: VFS path (e.g., "/nexus/streams/my-stream"). Must start with "/".
            capacity: Linear buffer byte capacity. Default 64KB.
            owner_id: Owner for ReBAC permission checks.

        Returns:
            The created StreamBuffer.

        Raises:
            StreamError: Stream already exists at this path.
        """
        from nexus.contracts.metadata import DT_STREAM, FileMetadata

        if path in self._buffers:
            raise StreamError(f"stream already exists: {path}")

        # Check if inode already exists in metastore
        existing = self._metastore.get(path)
        if existing is not None:
            raise StreamError(f"path already exists: {path}")

        # Create DT_STREAM inode in MetastoreABC.
        # Embed origin address so remote nodes can proxy stream I/O.
        # "stream" (no origin) = single-node mode, "stream@host:port" = federated.
        stream_backend = f"stream@{self._self_address}" if self._self_address else "stream"
        metadata = FileMetadata(
            path=path,
            backend_name=stream_backend,
            physical_path="mem://",
            size=capacity,
            entry_type=DT_STREAM,
            zone_id=zone_id,
            owner_id=owner_id,
        )
        self._metastore.put(metadata)

        # Create in-memory linear buffer
        buf = StreamBuffer(capacity=capacity)
        self._buffers[path] = buf

        logger.debug("stream created: %s (capacity=%d)", path, capacity)
        return buf

    def open(self, path: str, *, capacity: int = 65_536) -> StreamBackend:
        """Open an existing stream, or recover its buffer after restart.

        If the buffer is already in memory, returns it. If a DT_STREAM inode
        exists but the buffer was lost (process restart), creates a new
        buffer for the existing inode.

        Args:
            path: VFS path of the stream.
            capacity: Buffer capacity (used only if recreating after restart).

        Returns:
            The StreamBuffer for this stream.

        Raises:
            StreamNotFoundError: No stream inode at this path.
        """
        from nexus.contracts.metadata import DT_STREAM

        # Return existing buffer if available
        if path in self._buffers and not self._buffers[path].closed:
            return self._buffers[path]

        # Check metastore for inode
        metadata = self._metastore.get(path)
        if metadata is None or metadata.entry_type != DT_STREAM:
            raise StreamNotFoundError(f"no stream at: {path}")

        # Recreate buffer (restart recovery)
        buf = StreamBuffer(capacity=capacity)
        self._buffers[path] = buf

        logger.debug("stream opened (recovered): %s", path)
        return buf

    def create_from_backend(
        self,
        path: str,
        backend: StreamBackend,
        *,
        owner_id: str | None = None,
        zone_id: str = ROOT_ZONE_ID,
    ) -> StreamBackend:
        """Create a named stream backed by an external StreamBackend.

        Unlike ``create()`` which always uses an in-process StreamBuffer,
        this method accepts any StreamBackend implementation (e.g., a future
        SharedMemoryStreamBackend for inter-process IPC).

        The DT_STREAM inode is still registered in MetastoreABC for VFS
        visibility and ReBAC.

        Args:
            path: VFS path. Must start with "/".
            backend: The StreamBackend instance to use for data transport.
            owner_id: Owner for ReBAC permission checks.

        Returns:
            The registered StreamBackend (same object passed in).

        Raises:
            StreamError: Stream already exists at this path.
        """
        from nexus.contracts.metadata import DT_STREAM, FileMetadata

        if path in self._buffers:
            raise StreamError(f"stream already exists: {path}")

        existing = self._metastore.get(path)
        if existing is not None:
            raise StreamError(f"path already exists: {path}")

        stream_backend_name = f"stream@{self._self_address}" if self._self_address else "stream"
        metadata = FileMetadata(
            path=path,
            backend_name=stream_backend_name,
            physical_path="mem://",
            size=0,
            entry_type=DT_STREAM,
            zone_id=zone_id,
            owner_id=owner_id,
        )
        self._metastore.put(metadata)

        self._buffers[path] = backend
        logger.debug("stream created (custom backend): %s", path)
        return backend

    def signal_close(self, path: str) -> None:
        """Signal a stream closed without removing from registry.

        Closes the StreamBuffer (wakes blocked writers) but keeps it in
        ``_buffers`` so readers can still read existing data at their offsets.

        Raises:
            StreamNotFoundError: No buffer at this path.
        """
        buf = self._buffers.get(path)
        if buf is None:
            raise StreamNotFoundError(f"no stream at: {path}")
        buf.close()
        logger.debug("stream signal_close: %s", path)

    def close(self, path: str) -> None:
        """Close a stream's buffer and remove from registry. Inode stays in MetastoreABC.

        Raises:
            StreamNotFoundError: No buffer at this path.
        """
        buf = self._buffers.pop(path, None)
        if buf is None:
            raise StreamNotFoundError(f"no stream at: {path}")
        buf.close()
        self._locks.pop(path, None)
        logger.debug("stream closed: %s", path)

    def destroy(self, path: str) -> None:
        """Close buffer AND delete inode from MetastoreABC.

        Raises:
            StreamNotFoundError: No stream at this path.
        """
        buf = self._buffers.pop(path, None)
        if buf is not None:
            buf.close()
        self._locks.pop(path, None)

        metadata = self._metastore.get(path)
        if metadata is None:
            if buf is None:
                raise StreamNotFoundError(f"no stream at: {path}")
            return

        self._metastore.delete(path)
        logger.debug("stream destroyed: %s", path)

    def _get_buffer(self, path: str) -> StreamBackend:
        """Get buffer or raise StreamNotFoundError."""
        buf = self._buffers.get(path)
        if buf is None:
            raise StreamNotFoundError(f"no stream at: {path}")
        return buf

    def _get_lock(self, path: str) -> asyncio.Lock:
        """Get or create per-stream lock for concurrent writer safety."""
        lock = self._locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[path] = lock
        return lock

    # ------------------------------------------------------------------
    # Data path — writes locked, reads lock-free
    # ------------------------------------------------------------------

    async def stream_write(self, path: str, data: bytes, *, blocking: bool = True) -> int:
        """Write to a named stream. MPMC-safe (per-stream asyncio.Lock).

        Returns the byte offset where the message was appended.
        """
        buf = self._get_buffer(path)
        lock = self._get_lock(path)
        async with lock:
            return await buf.write(data, blocking=blocking)

    def stream_write_nowait(self, path: str, data: bytes) -> int:
        """Synchronous non-blocking write to a named stream.

        Atomic under asyncio event loop (no await points = no preemption).
        """
        return self._get_buffer(path).write_nowait(data)

    def stream_read_at(self, path: str, byte_offset: int = 0) -> tuple[bytes, int]:
        """Read one message at byte_offset. Lock-free (non-destructive).

        Returns (data, next_offset).
        """
        return self._get_buffer(path).read_at(byte_offset)

    async def stream_read(
        self, path: str, byte_offset: int = 0, *, blocking: bool = True
    ) -> tuple[bytes, int]:
        """Async read one message. Blocks until data at offset is available.

        No lock needed — reads are non-destructive and lock-free.
        Returns (data, next_offset).
        """
        return await self._get_buffer(path).read(byte_offset, blocking=blocking)

    def stream_read_batch(
        self, path: str, byte_offset: int = 0, count: int = 10
    ) -> tuple[list[bytes], int]:
        """Read up to `count` messages starting at byte_offset. Lock-free.

        Returns (list_of_bytes, next_offset).
        """
        return self._get_buffer(path).read_batch(byte_offset, count)

    async def stream_read_batch_blocking(
        self, path: str, byte_offset: int = 0, count: int = 10, *, blocking: bool = True
    ) -> tuple[list[bytes], int]:
        """Async read up to `count` messages. Blocks until at least one available.

        Returns (list_of_bytes, next_offset).
        """
        return await self._get_buffer(path).read_batch_blocking(
            byte_offset, count, blocking=blocking
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def list_streams(self) -> dict[str, dict]:
        """List all active streams with their stats."""
        return {path: buf.stats for path, buf in self._buffers.items()}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close_all(self) -> None:
        """Close all stream buffers. Called on kernel shutdown."""
        for path, buf in self._buffers.items():
            buf.close()
            logger.debug("stream closed (shutdown): %s", path)
        self._buffers.clear()
        self._locks.clear()
