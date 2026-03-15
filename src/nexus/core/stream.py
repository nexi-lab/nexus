"""DT_STREAM kernel IPC primitive — append-only log with offset-based reads.

Complements DT_PIPE (FIFO, destructive reads) as the second kernel messaging
primitive from KERNEL-ARCHITECTURE.md §4.2:

    | Primitive  | Linux Analogue   | Nexus                | Read      |
    |------------|------------------|----------------------|-----------|
    | DT_PIPE    | kfifo ring buffer| RingBuffer (pipe.py) | Destructive|
    | DT_STREAM  | append-only log  | StreamBuffer         | Non-destructive|

Multiple readers maintain independent cursors (fan-out). Primary use case:
LLM streaming I/O — realtime first consumer + replay for later consumers.

    stream.py           = kernel-internal buffer (kfifo equivalent)
    core/stream_manager.py = VFS named stream (fs/pipe.c equivalent)

Storage model (KERNEL-ARCHITECTURE.md):
    - Stream **inode** (FileMetadata, entry_type=DT_STREAM) → MetastoreABC
    - Stream **data** (bytes in linear buffer) → process heap (not in any pillar)

Data plane backed by Rust ``nexus_fast.StreamBufferCore``.
"""

import asyncio
import logging

try:
    from nexus_fast import StreamBufferCore
except ImportError:
    StreamBufferCore = None  # Rust extension not yet built; deferred until use

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StreamError(Exception):
    """Base exception for stream operations."""


class StreamFullError(StreamError):
    """Non-blocking write on a full buffer."""


class StreamEmptyError(StreamError):
    """Read at offset with no data available."""


class StreamClosedError(StreamError):
    """Operation on a closed stream."""


class StreamNotFoundError(StreamError):
    """No stream registered at the given path."""


# ---------------------------------------------------------------------------
# StreamBuffer — append-only log (kernel-internal, no VFS)
# ---------------------------------------------------------------------------


class StreamBuffer:
    """Append-only log buffer with offset-based non-destructive reads.

    Unlike RingBuffer (SPSC destructive FIFO), StreamBuffer is a linear
    append-only buffer where reads never consume data. Multiple readers
    maintain independent byte offsets (cursors).

    Data plane backed by Rust ``nexus_fast.StreamBufferCore``.
    Python provides asyncio.Event coordination for blocked writers.
    """

    __slots__ = ("_core", "_not_full")

    def __init__(self, capacity: int = 65_536) -> None:
        if StreamBufferCore is None:
            raise ImportError(
                "StreamBufferCore not available in nexus_fast — "
                "rebuild the Rust extension with stream support"
            )
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self._core = StreamBufferCore(capacity)
        self._not_full = asyncio.Event()
        self._not_full.set()  # initially not full

    # -- write (append) -----------------------------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Synchronous non-blocking append. Returns byte offset of the message.

        Raises:
            StreamFullError: Buffer is full (linear — never reclaims space).
            StreamClosedError: Buffer is closed.
            ValueError: Message larger than total capacity.
        """
        try:
            return int(self._core.push(data))
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise
        except ValueError:
            raise

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Async append. If blocking=True, waits for close (only way to unblock)."""
        while True:
            try:
                return self.write_nowait(data)
            except StreamFullError:
                if not blocking:
                    raise
                self._not_full.clear()
                await self._not_full.wait()
                if self._core.closed:
                    raise StreamClosedError("write to closed stream") from None

    # -- read (non-destructive, offset-based) --------------------------------

    def read_at(self, byte_offset: int = 0) -> tuple[bytes, int]:
        """Read one message at byte_offset. Returns (data, next_offset).

        Non-destructive — the same offset can be re-read by any reader.

        Raises:
            StreamEmptyError: No data at this offset.
            StreamClosedError: Stream closed and no data at offset.
        """
        try:
            data, next_offset = self._core.read_at(byte_offset)
            return bytes(data), next_offset
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise

    def read_batch(self, byte_offset: int = 0, count: int = 10) -> tuple[list[bytes], int]:
        """Read up to `count` messages starting at byte_offset.

        Returns (list_of_bytes, next_offset).

        Raises:
            StreamEmptyError: No data at this offset.
            StreamClosedError: Stream closed and no data at offset.
        """
        try:
            items, next_offset = self._core.read_batch(byte_offset, count)
            return [bytes(b) for b in items], next_offset
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the buffer. Wakes blocked writers."""
        self._core.close()
        self._not_full.set()  # wake blocked writers (so they see closed)

    @property
    def closed(self) -> bool:
        return bool(self._core.closed)

    @property
    def stats(self) -> dict:
        """Buffer statistics for observability."""
        return dict(self._core.stats())

    @property
    def tail(self) -> int:
        """Current write position (monotonic byte offset)."""
        return int(self._core.tail)


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _translate_rust_error(exc: RuntimeError) -> None:
    """Translate Rust RuntimeError tags to Python exception classes."""
    msg = str(exc)
    if msg.startswith("StreamClosed:"):
        raise StreamClosedError(msg.split(":", 1)[1]) from None
    if msg.startswith("StreamFull:"):
        raise StreamFullError(msg.split(":", 1)[1]) from None
    if msg.startswith("StreamEmpty:"):
        raise StreamEmptyError(msg.split(":", 1)[1]) from None
    raise exc
