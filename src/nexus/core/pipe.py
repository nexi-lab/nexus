"""DT_PIPE kernel IPC primitive — SPSC message-oriented ring buffer (kfifo).

Implements the Kernel messaging tier from KERNEL-ARCHITECTURE.md §6:

    | Tier       | Linux Analogue   | Nexus                              | Latency |
    |------------|------------------|------------------------------------|---------|
    | **Kernel** | kfifo ring buffer| Nexus Native Pipe (DT_PIPE)        | ~0.5μs  |

This file contains the kernel-internal ring buffer (kfifo equivalent).
For VFS-visible named pipes (mkfifo/fs/pipe.c equivalent), see
core/pipe_manager.py.

    pipe.py         = kfifo     (include/linux/kfifo.h + lib/kfifo.c)
    core/pipe_manager.py = fs/pipe.c (VFS named pipe, kernel tier)

Storage model (KERNEL-ARCHITECTURE.md line 228):
    - Pipe **inode** (FileMetadata, entry_type=DT_PIPE) → MetastoreABC
    - Pipe **data** (bytes in ring buffer) → process heap (not in any pillar)

Design: SPSC (single-producer, single-consumer) message-oriented buffer with
byte-capacity tracking. Data plane in Rust (nexus_fast.RingBufferCore) for
~0.5μs/op. asyncio.Event pairs provide blocking semantics from Python.

See: federation-memo.md §7j
"""

import asyncio
import logging

from nexus_fast import RingBufferCore

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

    For VFS-visible named pipes (mkfifo equivalent), use PipeManager
    from core/pipe_manager.py.

    Data plane backed by Rust ``nexus_fast.RingBufferCore`` (~0.5μs/op).
    Python provides asyncio.Event coordination for blocking read/write.
    """

    __slots__ = (
        "_core",
        "_loop",
        "_not_empty",
        "_not_full",
        "_readers_waiting",
        "_writers_waiting",
    )

    def __init__(self, capacity: int = 65_536) -> None:
        """Create a ring buffer with the given byte capacity.

        Args:
            capacity: Maximum total bytes across all buffered messages.
                      Default 64KB. Must be > 0.
        """
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self._core = RingBufferCore(capacity)
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()  # initially not full
        self._readers_waiting = False
        self._writers_waiting = False
        # Capture the event loop for cross-thread signalling.
        # RPC handlers run in a thread pool, so write_nowait() must use
        # call_soon_threadsafe() to wake the async consumer.
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # -- async write/read ---------------------------------------------------

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
        while True:
            try:
                return self.write_nowait(data)
            except PipeFullError:
                if not blocking:
                    raise
                self._writers_waiting = True
                self._not_full.clear()
                await self._not_full.wait()
                self._writers_waiting = False
                if self._core.closed:
                    raise PipeClosedError("write to closed pipe") from None

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
        while True:
            try:
                return self.read_nowait()
            except PipeEmptyError:
                if not blocking:
                    raise
                self._readers_waiting = True
                self._not_empty.clear()
                await self._not_empty.wait()
                self._readers_waiting = False

    # -- sync nowait --------------------------------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Synchronous non-blocking write. Raises PipeFullError if full.

        Thread-safe: may be called from RPC worker threads.  Uses
        ``call_soon_threadsafe`` so the asyncio.Event signal reaches the
        consumer task on the event-loop thread.
        """
        try:
            n = self._core.push(data)
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise  # unreachable, but keeps type checker happy
        except ValueError:
            raise  # oversized message — already a ValueError from Rust

        # Wake blocked reader.  The writer may run on a thread-pool
        # thread (RPC dispatch), so Event.set() must be delivered to
        # the event-loop thread via call_soon_threadsafe.
        if self._readers_waiting:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._not_empty.set)
            else:
                self._not_empty.set()
        return int(n)

    def read_nowait(self) -> bytes:
        """Synchronous non-blocking read. Raises PipeEmptyError if empty."""
        try:
            msg: bytes = self._core.pop()  # PyO3 returns bytes natively
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise  # unreachable

        # Wake blocked writer only if one is actually waiting.
        # Skipping Event.set() when no writer is blocked saves ~55ns/op.
        if self._writers_waiting:
            self._not_full.set()
        return msg

    # -- u64 fast path (L2 — zero PyBytes allocation) ----------------------

    def write_u64_nowait(self, val: int) -> None:
        """Push a u64 into the ring (12-byte frame). Zero PyBytes allocation."""
        try:
            self._core.push_u64(val)
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise  # unreachable
        except ValueError:
            raise

        if self._readers_waiting:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._not_empty.set)
            else:
                self._not_empty.set()

    def read_u64_nowait(self) -> int:
        """Pop a u64 from the ring. Returns Python int directly."""
        try:
            val: int = self._core.pop_u64()
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise  # unreachable

        if self._writers_waiting:
            self._not_full.set()
        return val

    async def write_u64(self, val: int, *, blocking: bool = True) -> None:
        """Async write a u64 value to the buffer."""
        while True:
            try:
                return self.write_u64_nowait(val)
            except PipeFullError:
                if not blocking:
                    raise
                self._writers_waiting = True
                self._not_full.clear()
                await self._not_full.wait()
                self._writers_waiting = False
                if self._core.closed:
                    raise PipeClosedError("write to closed pipe") from None

    async def read_u64(self, *, blocking: bool = True) -> int:
        """Async read a u64 value from the buffer."""
        while True:
            try:
                return self.read_u64_nowait()
            except PipeEmptyError:
                if not blocking:
                    raise
                self._readers_waiting = True
                self._not_empty.clear()
                await self._not_empty.wait()
                self._readers_waiting = False

    # -- wait helpers -------------------------------------------------------

    async def wait_writable(self) -> None:
        """Wait until buffer has space or is closed."""
        while self._core.is_full() and not self._core.closed:
            self._writers_waiting = True
            self._not_full.clear()
            await self._not_full.wait()
        self._writers_waiting = False

    async def wait_readable(self) -> None:
        """Wait until buffer has data or is closed."""
        while self._core.is_empty() and not self._core.closed:
            self._readers_waiting = True
            self._not_empty.clear()
            await self._not_empty.wait()
        self._readers_waiting = False

    # -- observability ------------------------------------------------------

    def peek(self) -> bytes | None:
        """Non-consuming peek at the next message. Returns None if empty."""
        result = self._core.peek()
        return bytes(result) if result is not None else None

    def peek_all(self) -> list[bytes]:
        """Non-consuming view of all buffered messages."""
        return [bytes(b) for b in self._core.peek_all()]

    def close(self) -> None:
        """Close the buffer. Wakes all blocked readers/writers."""
        self._core.close()
        self._not_empty.set()  # wake blocked readers
        self._not_full.set()  # wake blocked writers

    @property
    def closed(self) -> bool:
        return bool(self._core.closed)

    @property
    def stats(self) -> dict:
        """Buffer statistics for observability."""
        return dict(self._core.stats())


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _translate_rust_error(exc: RuntimeError) -> None:
    """Translate Rust RuntimeError tags to Python exception classes."""
    msg = str(exc)
    if msg.startswith("PipeClosed:"):
        raise PipeClosedError(msg.split(":", 1)[1]) from None
    if msg.startswith("PipeFull:"):
        raise PipeFullError(msg.split(":", 1)[1]) from None
    if msg.startswith("PipeEmpty:"):
        raise PipeEmptyError(msg.split(":", 1)[1]) from None
    # Unknown RuntimeError — re-raise as-is
    raise exc
