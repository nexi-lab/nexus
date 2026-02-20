"""RingBuffer with Rust acceleration for DT_PIPE hot path (Issue #806, Phase 2).

Rust ``nexus_fast.RingBuffer`` handles the sync data path (~50-100ns per op).
Python ``AsyncRingBuffer`` wraps with ``asyncio.Event`` for blocking semantics.

This is architecturally correct — async signaling belongs to the Python event
loop, the data structure is pure computation that benefits from Rust. Same
split as ``lock_fast.py`` / ``lock.rs``.

See: pipe.py for Python-only RingBuffer (tests/CLI), pipe_manager.py for VFS named pipes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from nexus.core.pipe import (
    PipeClosedError,
    PipeEmptyError,
    PipeFullError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class RingBufferSyncProtocol(Protocol):
    """Sync-only interface — satisfied by Rust ``nexus_fast.RingBuffer``."""

    def write_nowait(self, data: bytes) -> int: ...

    def read_nowait(self) -> bytes: ...

    def peek(self) -> bytes | None: ...

    def peek_all(self) -> list[bytes]: ...

    def close(self) -> None: ...

    @property
    def closed(self) -> bool: ...

    @property
    def stats(self) -> dict: ...


@runtime_checkable
class RingBufferProtocol(RingBufferSyncProtocol, Protocol):
    """Full protocol with async — satisfied by ``AsyncRingBuffer``."""

    async def write(self, data: bytes, *, blocking: bool = True) -> int: ...

    async def read(self, *, blocking: bool = True) -> bytes: ...

    async def wait_writable(self) -> None: ...

    async def wait_readable(self) -> None: ...


# ---------------------------------------------------------------------------
# AsyncRingBuffer — wraps Rust sync backend with asyncio.Event signaling
# ---------------------------------------------------------------------------


class AsyncRingBuffer:
    """Adds ``asyncio.Event`` signaling on top of a Rust sync backend.

    The Rust backend owns the VecDeque and byte tracking (~50-100ns per op).
    This layer owns the two asyncio.Events (``_not_empty``, ``_not_full``).

    Why: asyncio.Event must live in the Python event loop. Rust cannot safely
    signal asyncio primitives. By separating, we get Rust speed for sync ops
    and correct asyncio semantics for blocking ops.
    """

    __slots__ = ("_backend", "_capacity", "_not_empty", "_not_full")

    def __init__(self, backend: RingBufferSyncProtocol, capacity: int) -> None:
        self._backend = backend
        self._capacity = capacity
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()  # initially not full

    # -- sync hot path (delegated to backend) ------------------------------

    def write_nowait(self, data: bytes) -> int:
        """Sync non-blocking write. Manages event signaling."""
        n = self._backend.write_nowait(data)
        if n > 0:
            self._not_empty.set()
            if self._backend.stats["size"] >= self._capacity:
                self._not_full.clear()
        return n

    def read_nowait(self) -> bytes:
        """Sync non-blocking read. Manages event signaling."""
        msg = self._backend.read_nowait()
        self._not_full.set()
        if self._backend.stats["msg_count"] == 0:
            self._not_empty.clear()
        return msg

    def peek(self) -> bytes | None:
        return self._backend.peek()

    def peek_all(self) -> list[bytes]:
        return self._backend.peek_all()

    def close(self) -> None:
        self._backend.close()
        self._not_empty.set()  # wake blocked readers
        self._not_full.set()  # wake blocked writers

    @property
    def closed(self) -> bool:
        return self._backend.closed

    @property
    def stats(self) -> dict:
        return self._backend.stats

    # -- async methods (blocking semantics via events) ---------------------

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Async write with optional blocking."""
        if self._backend.closed:
            raise PipeClosedError("write to closed pipe")
        if not data:
            return 0
        msg_len = len(data)
        if msg_len > self._capacity:
            raise ValueError(f"message size {msg_len} exceeds buffer capacity {self._capacity}")
        while True:
            try:
                return self.write_nowait(data)
            except PipeFullError:
                if not blocking:
                    raise
                self._not_full.clear()
                await self._not_full.wait()
                if self._backend.closed:
                    raise PipeClosedError("write to closed pipe") from None

    async def read(self, *, blocking: bool = True) -> bytes:
        """Async read with optional blocking."""
        while True:
            try:
                return self.read_nowait()
            except PipeEmptyError:
                if self._backend.closed:
                    raise PipeClosedError("read from closed empty pipe") from None
                if not blocking:
                    raise
                self._not_empty.clear()
                await self._not_empty.wait()
            except PipeClosedError:
                raise

    async def wait_writable(self) -> None:
        """Wait until buffer has space or is closed."""
        while self._backend.stats["size"] >= self._capacity and not self._backend.closed:
            self._not_full.clear()
            await self._not_full.wait()

    async def wait_readable(self) -> None:
        """Wait until buffer has data or is closed."""
        while self._backend.stats["msg_count"] == 0 and not self._backend.closed:
            self._not_empty.clear()
            await self._not_empty.wait()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_ring_buffer(capacity: int = 65_536) -> AsyncRingBuffer:
    """Create a Rust-accelerated RingBuffer wrapped with asyncio signaling.

    Requires ``nexus_fast`` (Rust extension). Fails loudly if unavailable —
    platform adaptation is a DI concern, not a kernel fallback.
    """
    from nexus_fast import RingBuffer as _RustRB

    return AsyncRingBuffer(_RustRB(capacity), capacity)
