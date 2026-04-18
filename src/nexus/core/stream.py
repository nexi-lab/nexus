"""DT_STREAM kernel IPC primitive — append-only log with offset-based reads.

Complements DT_PIPE (FIFO, destructive reads) as the second kernel messaging
primitive from KERNEL-ARCHITECTURE.md §4.2:

    | Primitive  | Linux Analogue   | Nexus                     | Read           |
    |------------|------------------|---------------------------|----------------|
    | DT_PIPE    | kfifo ring buffer| Rust kernel IPC registry  | Destructive    |
    | DT_STREAM  | append-only log  | Rust kernel IPC registry  | Non-destructive|

Multiple readers maintain independent cursors (fan-out). Primary use case:
LLM streaming I/O — realtime first consumer + replay for later consumers.

This file defines the StreamBackend protocol and exception classes for DT_STREAM.
The actual data plane lives in the Rust kernel IPC registry (DashMap<String, StreamBufferCore>).

Storage model (KERNEL-ARCHITECTURE.md):
    - Stream **inode** (FileMetadata, entry_type=DT_STREAM) → MetastoreABC
    - Stream **data** (bytes in linear buffer) → Rust kernel IPC registry (not in any pillar)
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

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
        Rust kernel IPC registry                  — in-process append-only buffer (~0.5μs)
        Rust ``SharedMemoryStreamBackend``        — cross-process mmap'd linear buffer (~1–5μs)
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
