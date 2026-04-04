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

Data plane backed by Rust ``nexus_kernel.StreamBufferCore``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

# RUST_FALLBACK: StreamBufferCore
from nexus._rust_compat import StreamBufferCore as _StreamBufferCoreType

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
# StreamBackend protocol — pluggable transport tier
# ---------------------------------------------------------------------------


@runtime_checkable
class StreamBackend(Protocol):
    """Protocol for stream data transport backends.

    Pluggable transport tier for DT_STREAM (KERNEL-ARCHITECTURE.md §4.2).
    StreamManager stores ``dict[str, StreamBackend]`` — all backends share
    this interface so StreamManager is transport-agnostic.

    Implementations:
        StreamBuffer            — in-process append-only buffer (Rust, ~0.5μs)
        SharedStreamBuffer (shm_stream.py) — cross-process mmap'd linear buffer (~1–5μs)
    """

    async def write(self, data: bytes, *, blocking: bool = True) -> int: ...
    def write_nowait(self, data: bytes) -> int: ...
    def read_at(self, byte_offset: int = 0) -> tuple[bytes, int]: ...
    async def read(self, byte_offset: int = 0, *, blocking: bool = True) -> tuple[bytes, int]: ...
    def read_batch(self, byte_offset: int = 0, count: int = 10) -> tuple[list[bytes], int]: ...
    async def read_batch_blocking(
        self, byte_offset: int = 0, count: int = 10, *, blocking: bool = True
    ) -> tuple[list[bytes], int]: ...
    def close(self) -> None: ...

    @property
    def closed(self) -> bool: ...

    @property
    def stats(self) -> dict: ...

    @property
    def tail(self) -> int: ...


# ---------------------------------------------------------------------------
# StreamBuffer — append-only log (kernel-internal, no VFS)
# ---------------------------------------------------------------------------


class StreamBuffer:
    """Append-only log buffer with offset-based non-destructive reads.

    Unlike RingBuffer (SPSC destructive FIFO), StreamBuffer is a linear
    append-only buffer where reads never consume data. Multiple readers
    maintain independent byte offsets (cursors).

    Data plane backed by Rust ``nexus_kernel.StreamBufferCore``.
    Python provides asyncio.Event coordination for blocked writers.
    """

    __slots__ = ("_core", "_not_empty", "_not_full")

    def __init__(self, capacity: int = 65_536) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if _StreamBufferCoreType is None:
            raise RuntimeError(
                "Stream requires the nexus-kernel Rust extension. "
                "Install nexus-ai-fs or rebuild: pip install -e rust/nexus_pyo3"
            )
        self._core = _StreamBufferCoreType(capacity)
        self._not_empty = asyncio.Event()
        # _not_empty starts unset — no data yet
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
            offset = int(self._core.push(data))
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise
        except ValueError:
            raise
        # Wake blocked readers — new data is available
        self._not_empty.set()
        return offset

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

    # -- async blocking reads -----------------------------------------------

    async def read(self, byte_offset: int = 0, *, blocking: bool = True) -> tuple[bytes, int]:
        """Async read one message at byte_offset. Blocks until data is available.

        If blocking=True and no data at offset, waits for a push() or close().
        Non-destructive — same offset can be re-read.

        Returns (data, next_offset).

        Raises:
            StreamEmptyError: Non-blocking and no data at offset.
            StreamClosedError: Stream closed and no data at offset.
        """
        while True:
            try:
                return self.read_at(byte_offset)
            except StreamEmptyError:
                if not blocking:
                    raise
                if self._core.closed:
                    raise StreamClosedError(
                        f"stream closed, no data at offset {byte_offset}"
                    ) from None
                self._not_empty.clear()
                # Re-check before sleeping (avoid lost-wakeup race)
                try:
                    return self.read_at(byte_offset)
                except StreamEmptyError:
                    pass
                await self._not_empty.wait()

    async def read_batch_blocking(
        self, byte_offset: int = 0, count: int = 10, *, blocking: bool = True
    ) -> tuple[list[bytes], int]:
        """Async read up to `count` messages. Blocks until at least one available.

        Returns (list_of_bytes, next_offset).
        """
        while True:
            try:
                return self.read_batch(byte_offset, count)
            except StreamEmptyError:
                if not blocking:
                    raise
                if self._core.closed:
                    raise StreamClosedError(
                        f"stream closed, no data at offset {byte_offset}"
                    ) from None
                self._not_empty.clear()
                try:
                    return self.read_batch(byte_offset, count)
                except StreamEmptyError:
                    pass
                await self._not_empty.wait()

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the buffer. Wakes blocked readers and writers."""
        self._core.close()
        self._not_empty.set()  # wake blocked readers (so they see closed)
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
