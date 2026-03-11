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

See: federation-memo.md §7j, ISSUE-A2A-PHASE2-VFS-IPC.md
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
        self._core = RingBufferCore(capacity)
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()  # initially not full

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
                # Only clear+wait here — nowait path skips is_full() check
                self._not_full.clear()
                await self._not_full.wait()
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
                # Only clear+wait here — nowait path skips is_empty() check
                self._not_empty.clear()
                await self._not_empty.wait()

    # -- sync nowait --------------------------------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Synchronous non-blocking write. Raises PipeFullError if full."""
        try:
            n = self._core.push(data)
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise  # unreachable, but keeps type checker happy
        except ValueError:
            raise  # oversized message — already a ValueError from Rust

        # Wake blocked reader. Event.clear() deferred to async write() path
        # — avoids is_full() FFI call on every sync write.
        self._not_empty.set()
        return int(n)

    def read_nowait(self) -> bytes:
        """Synchronous non-blocking read. Raises PipeEmptyError if empty."""
        try:
            msg: bytes = self._core.pop()  # PyO3 returns bytes natively
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise  # unreachable

        # Wake blocked writer. Event.clear() deferred to async read() path
        # — avoids is_empty() FFI call on every sync read.
        self._not_full.set()
        return msg

    # -- wait helpers -------------------------------------------------------

    async def wait_writable(self) -> None:
        """Wait until buffer has space or is closed."""
        while self._core.is_full() and not self._core.closed:
            self._not_full.clear()
            await self._not_full.wait()

    async def wait_readable(self) -> None:
        """Wait until buffer has data or is closed."""
        while self._core.is_empty() and not self._core.closed:
            self._not_empty.clear()
            await self._not_empty.wait()

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
