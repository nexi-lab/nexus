"""DT_PIPE kernel IPC primitive — SPSC message-oriented ring buffer (kfifo).

Implements the Kernel messaging tier from KERNEL-ARCHITECTURE.md §6:

    | Tier       | Linux Analogue   | Nexus                              | Latency |
    |------------|------------------|------------------------------------|---------|
    | **Kernel** | kfifo ring buffer| Nexus Native Pipe (DT_PIPE)        | ~5μs    |

This file contains the kernel-internal ring buffer (kfifo equivalent).
For VFS-visible named pipes (mkfifo/fs/pipe.c equivalent), see
system_services/pipe_manager.py (moved from core/ per Issue #2366).

    pipe.py         = kfifo     (include/linux/kfifo.h + lib/kfifo.c)
    system_services/pipe_manager.py = fs/pipe.c (VFS named pipe, system service tier)

Storage model (KERNEL-ARCHITECTURE.md line 228):
    - Pipe **inode** (FileMetadata, entry_type=DT_PIPE) → MetastoreABC
    - Pipe **data** (bytes in ring buffer) → process heap (not in any pillar)

Design: SPSC (single-producer, single-consumer) message-oriented deque with
byte-capacity tracking. No explicit lock — CPython deque.append/popleft are
GIL-atomic for SPSC. asyncio.Event pairs provide blocking semantics.

Phase 1 = Python (this file). Phase 2 = Rust lock-free SPSC via nexus_fast (Task #806).

See: federation-memo.md §7j, ISSUE-A2A-PHASE2-VFS-IPC.md
"""

import asyncio
import logging
from collections import deque

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


class PipeTimeoutError(PipeError):
    """Pipe operation timed out."""


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

    For VFS-visible named pipes (mkfifo equivalent), use PipeManager
    from system_services/pipe_manager.py.

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

    async def write(
        self, data: bytes, *, blocking: bool = True, timeout: float | None = None
    ) -> int:
        """Write a message to the buffer.

        Args:
            data: Message bytes. Empty bytes (b"") are silently ignored.
            blocking: If True, wait until space is available.
                      If False, raise PipeFullError immediately.
            timeout: Maximum seconds to wait (None = wait forever).
                     Only meaningful when blocking=True.

        Returns:
            Number of bytes written.

        Raises:
            PipeClosedError: Buffer is closed.
            PipeFullError: Non-blocking and buffer is full.
            PipeTimeoutError: Timed out waiting for space.
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
            # Re-check after clear to avoid lost-wakeup race
            if self._size + msg_len <= self._capacity:
                break
            if timeout is not None:
                try:
                    await asyncio.wait_for(self._not_full.wait(), timeout=timeout)
                except TimeoutError:
                    raise PipeTimeoutError("write timed out") from None
            else:
                await self._not_full.wait()
            if self._closed:
                raise PipeClosedError("write to closed pipe")

        self._buf.append(data)
        self._size += msg_len
        self._not_empty.set()

        if self._size >= self._capacity:
            self._not_full.clear()

        return msg_len

    async def read(self, *, blocking: bool = True, timeout: float | None = None) -> bytes:
        """Read the next message from the buffer.

        Args:
            blocking: If True, wait until a message is available.
                      If False, raise PipeEmptyError immediately.
            timeout: Maximum seconds to wait (None = wait forever).
                     Only meaningful when blocking=True.

        Returns:
            The next message bytes.

        Raises:
            PipeClosedError: Buffer is closed AND empty.
            PipeEmptyError: Non-blocking and buffer is empty.
            PipeTimeoutError: Timed out waiting for data.
        """
        while not self._buf:
            if self._closed:
                raise PipeClosedError("read from closed empty pipe")
            if not blocking:
                raise PipeEmptyError("buffer empty")
            self._not_empty.clear()
            # Re-check after clear to avoid lost-wakeup race
            if self._buf:
                break
            if timeout is not None:
                try:
                    await asyncio.wait_for(self._not_empty.wait(), timeout=timeout)
                except TimeoutError:
                    raise PipeTimeoutError("read timed out") from None
            else:
                await self._not_empty.wait()

        msg = self._buf.popleft()
        self._size -= len(msg)
        self._not_full.set()

        if not self._buf:
            self._not_empty.clear()

        return msg

    def write_nowait(self, data: bytes) -> int:
        """Synchronous non-blocking write. Raises PipeFullError if full.

        Same logic as write(blocking=False) but callable from sync code.
        Used by PipeManager.pipe_write_nowait() for sync producers.
        """
        if self._closed:
            raise PipeClosedError("write to closed pipe")
        if not data:
            return 0
        msg_len = len(data)
        if msg_len > self._capacity:
            raise ValueError(f"message size {msg_len} exceeds buffer capacity {self._capacity}")
        if self._size + msg_len > self._capacity:
            raise PipeFullError(f"buffer full ({self._size}/{self._capacity} bytes)")
        self._buf.append(data)
        self._size += msg_len
        self._not_empty.set()
        if self._size >= self._capacity:
            self._not_full.clear()
        return msg_len

    def read_nowait(self) -> bytes:
        """Synchronous non-blocking read. Raises PipeEmptyError if empty.

        Same logic as read(blocking=False) but callable from sync code.
        Used by PipeManager.pipe_read() under lock for MPMC safety.
        """
        if not self._buf:
            if self._closed:
                raise PipeClosedError("read from closed empty pipe")
            raise PipeEmptyError("buffer empty")
        msg = self._buf.popleft()
        self._size -= len(msg)
        self._not_full.set()
        if not self._buf:
            self._not_empty.clear()
        return msg

    async def wait_writable(self) -> None:
        """Wait until buffer has space or is closed.

        Public interface to internal Event state. Used by PipeManager
        for lock→try→unlock→wait→retry pattern (avoids exposing _not_full).
        """
        while self._size >= self._capacity and not self._closed:
            self._not_full.clear()
            # Re-check after clear to avoid lost-wakeup race
            if self._size < self._capacity or self._closed:
                break
            await self._not_full.wait()

    async def wait_readable(self) -> None:
        """Wait until buffer has data or is closed.

        Public interface to internal Event state. Used by PipeManager
        for lock→try→unlock→wait→retry pattern (avoids exposing _not_empty).
        """
        while not self._buf and not self._closed:
            self._not_empty.clear()
            # Re-check after clear to avoid lost-wakeup race
            if self._buf or self._closed:
                break
            await self._not_empty.wait()

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
