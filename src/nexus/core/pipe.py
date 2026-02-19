"""DT_PIPE kernel IPC primitive — SPSC message-oriented ring buffer.

Implements the Kernel messaging tier from KERNEL-ARCHITECTURE.md §6:

    | Tier       | Linux Analogue   | Nexus                              | Latency |
    |------------|------------------|------------------------------------|---------|
    | **Kernel** | kfifo ring buffer| Nexus Native Pipe (DT_PIPE)        | ~5μs    |

Two layers, mirroring Linux:

    RingBuffer  = kfifo equivalent (kernel-internal, no VFS path, any code can use directly)
    PipeManager = mkfifo equivalent (VFS-visible named pipe, inode in MetastoreABC)

Storage model (KERNEL-ARCHITECTURE.md line 228):
    - Pipe **inode** (FileMetadata, entry_type=DT_PIPE) → MetastoreABC
    - Pipe **data** (bytes in ring buffer) → process heap (not in any pillar)

Design: SPSC (single-producer, single-consumer) message-oriented deque with
byte-capacity tracking. No explicit lock — CPython deque.append/popleft are
GIL-atomic for SPSC. asyncio.Event pairs provide blocking semantics.

Phase 1 = Python (this file). Phase 2 = Rust lock-free SPSC via nexus_fast (Task #806).

See: federation-memo.md §7j, ISSUE-A2A-PHASE2-VFS-IPC.md
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PipeError(Exception):
    """Base exception for pipe operations."""


class PipeFullError(PipeError):
    """Non-blocking write on a full buffer."""


class PipeEmptyError(PipeError):
    """Non-blocking read on an empty buffer."""


class PipeClosedError(PipeError):
    """Operation on a closed pipe."""


class PipeNotFoundError(PipeError):
    """No pipe registered at the given path."""


# ---------------------------------------------------------------------------
# RingBuffer — kfifo equivalent (kernel-internal, no VFS)
# ---------------------------------------------------------------------------


class RingBuffer:
    """SPSC message-oriented ring buffer for kernel IPC.

    Analogous to Linux kfifo: a kernel-internal FIFO with no filesystem
    visibility. Any kernel code or in-process service can instantiate one
    directly for fast async signaling.

    For VFS-visible named pipes (mkfifo equivalent), use PipeManager.

    Design choices:
      - Message-oriented (deque of discrete bytes), not byte-stream.
        All real usage is discrete JSON messages (A2A tasks, agent commands).
      - No explicit lock. SPSC contract + CPython GIL makes deque
        append/popleft atomic. asyncio.Event pairs for blocking.
      - Byte-capacity tracking (not message count) for backpressure.
        A single A2A message is 500-2000 bytes; capacity in bytes is
        more meaningful than message count.

    Performance: ~5μs per operation (Python Phase 1).
    Phase 2 target: ~0.5μs via Rust lock-free SPSC (Task #806).
    """

    __slots__ = (
        "_capacity",
        "_buf",
        "_size",
        "_closed",
        "_not_empty",
        "_not_full",
    )

    def __init__(self, capacity: int = 65_536) -> None:
        """Create a ring buffer with the given byte capacity.

        Args:
            capacity: Maximum total bytes across all buffered messages.
                      Default 64KB. Must be > 0.
        """
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self._capacity = capacity
        self._buf: deque[bytes] = deque()
        self._size: int = 0
        self._closed: bool = False
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()  # initially not full

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Write a message to the buffer.

        Args:
            data: Message bytes. Empty bytes (b"") are silently ignored.
            blocking: If True, wait until space is available.
                      If False, raise PipeFullError immediately.

        Returns:
            Number of bytes written.

        Raises:
            PipeClosedError: Buffer is closed.
            PipeFullError: Non-blocking and buffer is full.
            ValueError: Message larger than total capacity.
        """
        if self._closed:
            raise PipeClosedError("write to closed pipe")

        if not data:
            return 0

        msg_len = len(data)
        if msg_len > self._capacity:
            raise ValueError(f"message size {msg_len} exceeds buffer capacity {self._capacity}")

        while self._size + msg_len > self._capacity:
            if not blocking:
                raise PipeFullError(f"buffer full ({self._size}/{self._capacity} bytes)")
            self._not_full.clear()
            # Wait for space or close
            await self._not_full.wait()
            if self._closed:
                raise PipeClosedError("write to closed pipe")

        self._buf.append(data)
        self._size += msg_len
        self._not_empty.set()

        if self._size >= self._capacity:
            self._not_full.clear()

        return msg_len

    async def read(self, *, blocking: bool = True) -> bytes:
        """Read the next message from the buffer.

        Args:
            blocking: If True, wait until a message is available.
                      If False, raise PipeEmptyError immediately.

        Returns:
            The next message bytes.

        Raises:
            PipeClosedError: Buffer is closed AND empty.
            PipeEmptyError: Non-blocking and buffer is empty.
        """
        while not self._buf:
            if self._closed:
                raise PipeClosedError("read from closed empty pipe")
            if not blocking:
                raise PipeEmptyError("buffer empty")
            self._not_empty.clear()
            await self._not_empty.wait()

        msg = self._buf.popleft()
        self._size -= len(msg)
        self._not_full.set()

        if not self._buf:
            self._not_empty.clear()

        return msg

    def peek(self) -> bytes | None:
        """Non-consuming peek at the next message. Returns None if empty."""
        return self._buf[0] if self._buf else None

    def peek_all(self) -> list[bytes]:
        """Non-consuming view of all buffered messages (for observability)."""
        return list(self._buf)

    def close(self) -> None:
        """Close the buffer. Wakes all blocked readers/writers.

        After close:
          - write() raises PipeClosedError
          - read() drains remaining messages, then raises PipeClosedError
        """
        self._closed = True
        self._not_empty.set()  # wake blocked readers
        self._not_full.set()  # wake blocked writers

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> dict:
        """Buffer statistics for observability."""
        return {
            "size": self._size,
            "capacity": self._capacity,
            "msg_count": len(self._buf),
            "closed": self._closed,
        }


# ---------------------------------------------------------------------------
# PipeManager — mkfifo equivalent (VFS-visible named pipes)
# ---------------------------------------------------------------------------


class PipeManager:
    """Manages DT_PIPE lifecycle and buffer registry.

    Analogous to mkfifo: creates named pipes visible in the VFS namespace.
    Each pipe has a FileMetadata inode in MetastoreABC (entry_type=DT_PIPE)
    and a RingBuffer in process memory.

    The inode provides:
      - VFS path (/nexus/pipes/{name}) for agent access via FUSE/MCP
      - ReBAC access control (owner_id, permission checks)
      - Observability (list all pipes, inspect stats)

    The ring buffer data is NOT in any storage pillar — it's process heap
    memory, like Linux kfifo data in kmalloc'd kernel heap.
    """

    def __init__(self, metastore: MetastoreABC, zone_id: str = "root") -> None:
        self._metastore = metastore
        self._zone_id = zone_id
        self._buffers: dict[str, RingBuffer] = {}

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
        from nexus.core.metadata import DT_PIPE, FileMetadata

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
        from nexus.core.metadata import DT_PIPE

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

    def close(self, path: str) -> None:
        """Close a pipe's buffer. Inode stays in MetastoreABC.

        Readers can still drain remaining messages. The inode persists
        so the pipe can be reopened later.

        Raises:
            PipeNotFoundError: No buffer at this path.
        """
        buf = self._buffers.pop(path, None)
        if buf is None:
            raise PipeNotFoundError(f"no pipe at: {path}")
        buf.close()
        logger.debug("pipe closed: %s", path)

    def destroy(self, path: str) -> None:
        """Close buffer AND delete inode from MetastoreABC.

        Raises:
            PipeNotFoundError: No pipe at this path.
        """
        buf = self._buffers.pop(path, None)
        if buf is not None:
            buf.close()

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

    async def pipe_write(self, path: str, data: bytes, *, blocking: bool = True) -> int:
        """Write to a named pipe."""
        return await self._get_buffer(path).write(data, blocking=blocking)

    async def pipe_read(self, path: str, *, blocking: bool = True) -> bytes:
        """Read from a named pipe."""
        return await self._get_buffer(path).read(blocking=blocking)

    def pipe_peek(self, path: str) -> bytes | None:
        """Peek at next message in a named pipe."""
        return self._get_buffer(path).peek()

    def pipe_peek_all(self, path: str) -> list[bytes]:
        """Peek at all messages in a named pipe."""
        return self._get_buffer(path).peek_all()

    def list_pipes(self) -> dict[str, dict]:
        """List all active pipes with their stats."""
        return {path: buf.stats for path, buf in self._buffers.items()}

    def close_all(self) -> None:
        """Close all pipe buffers. Called on kernel shutdown."""
        for path, buf in self._buffers.items():
            buf.close()
            logger.debug("pipe closed (shutdown): %s", path)
        self._buffers.clear()
