"""Cross-process DT_PIPE via shared memory (mmap) — SharedRingBuffer (#1680).

Implements ``PipeBackend`` protocol using ``SharedRingBufferCore`` (Rust, mmap)
for cross-process SPSC communication.  OS pipes provide async wakeup via
``loop.add_reader(fd, callback)``.

For in-process use, prefer ``RingBuffer`` from ``core/pipe.py`` (zero syscall).

Usage::

    # Parent (nexusd)
    buf, shm_path, data_rd_fd, space_rd_fd = SharedRingBuffer.create(65536)
    # pass shm_path + fds to child via env/args
    await buf.write(b"hello")

    # Child (worker)
    buf = SharedRingBuffer.attach(shm_path, data_wr_fd, space_wr_fd)
    msg = await buf.read()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Self

from nexus.core.pipe import PipeClosedError, PipeEmptyError, PipeFullError, _translate_rust_error

try:
    from nexus_fast import SharedRingBufferCore
except ImportError:
    SharedRingBufferCore = None

logger = logging.getLogger(__name__)


class SharedRingBuffer:
    """Cross-process SPSC ring buffer. Implements PipeBackend protocol.

    Uses mmap'd shared memory for zero-copy data plane and OS pipes for
    async notification.  Each push/pop incurs ~1-2μs syscall for the
    pipe notification (vs ~0.5μs for in-process RingBuffer).
    """

    __slots__ = (
        "_core",
        "_data_rd_fd",
        "_space_rd_fd",
        "_loop",
        "_not_empty",
        "_not_full",
    )

    def __init__(
        self,
        core: SharedRingBufferCore,
        data_rd_fd: int = -1,
        space_rd_fd: int = -1,
    ) -> None:
        self._core = core
        self._data_rd_fd = data_rd_fd
        self._space_rd_fd = space_rd_fd
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Register fd readers if we have an event loop
        if self._loop is not None:
            if self._data_rd_fd >= 0:
                self._loop.add_reader(self._data_rd_fd, self._on_data_available)
            if self._space_rd_fd >= 0:
                self._loop.add_reader(self._space_rd_fd, self._on_space_available)

    @classmethod
    def create(cls, capacity: int = 65_536) -> tuple[Self, str, int, int]:
        """Create a new shared ring buffer (parent/writer side).

        Returns:
            (buffer, shm_path, data_rd_fd, space_rd_fd)
            - shm_path: pass to child for attach()
            - data_rd_fd: pass to child (child listens for data notifications)
            - space_rd_fd: keep in parent (parent listens for space notifications)
        """
        if SharedRingBufferCore is None:
            raise ImportError("SharedRingBufferCore not available in nexus_fast")
        core, shm_path, data_rd_fd, space_rd_fd = SharedRingBufferCore.create(capacity)
        # Parent keeps space_rd_fd (wakes when reader frees space)
        buf = cls(core, data_rd_fd=-1, space_rd_fd=space_rd_fd)
        return buf, shm_path, data_rd_fd, space_rd_fd

    @classmethod
    def attach(
        cls, shm_path: str, notify_data_wr: int, notify_space_wr: int, data_rd_fd: int = -1
    ) -> Self:
        """Attach to an existing shared ring buffer (child/reader side).

        Args:
            shm_path: Path to the shared memory file.
            notify_data_wr: Write-end of data pipe (child writes here — passed to Rust core).
            notify_space_wr: Write-end of space pipe (child writes here after pop — passed to Rust core).
            data_rd_fd: Read-end of data pipe (child listens here for data notifications).
        """
        if SharedRingBufferCore is None:
            raise ImportError("SharedRingBufferCore not available in nexus_fast")
        core = SharedRingBufferCore.attach(shm_path, notify_data_wr, notify_space_wr)
        return cls(core, data_rd_fd=data_rd_fd, space_rd_fd=-1)

    # -- fd callbacks ---------------------------------------------------------

    def _on_data_available(self) -> None:
        """Called by event loop when data notification pipe is readable."""
        # Drain the pipe (non-blocking)
        with contextlib.suppress(OSError):
            os.read(self._data_rd_fd, 256)
        self._not_empty.set()

    def _on_space_available(self) -> None:
        """Called by event loop when space notification pipe is readable."""
        with contextlib.suppress(OSError):
            os.read(self._space_rd_fd, 256)
        self._not_full.set()

    # -- async write/read -----------------------------------------------------

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        while True:
            try:
                return self.write_nowait(data)
            except PipeFullError:
                if not blocking:
                    raise
                self._not_full.clear()
                await self._not_full.wait()
                if self._core.closed:
                    raise PipeClosedError("write to closed pipe") from None

    async def read(self, *, blocking: bool = True) -> bytes:
        while True:
            try:
                return self.read_nowait()
            except PipeEmptyError:
                if not blocking:
                    raise
                self._not_empty.clear()
                await self._not_empty.wait()

    # -- sync nowait ----------------------------------------------------------

    def write_nowait(self, data: bytes) -> int:
        try:
            n = self._core.push(data)
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise
        except ValueError:
            raise
        # Wake up any blocked reader (e.g. PipedRecordStoreWriteObserver consumer)
        self._not_empty.set()
        return int(n)

    def read_nowait(self) -> bytes:
        try:
            msg: bytes = self._core.pop()
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise
        return msg

    # -- wait helpers ---------------------------------------------------------

    async def wait_writable(self) -> None:
        while self._core.is_full() and not self._core.closed:
            self._not_full.clear()
            await self._not_full.wait()

    async def wait_readable(self) -> None:
        import asyncio as _asyncio

        while self._core.is_empty() and not self._core.closed:
            # Poll with short sleep instead of relying on Event.set() alone,
            # because producers may run on a different event loop (e.g. gRPC
            # async server vs. FastAPI/uvicorn loop). Event.set() from a
            # different loop doesn't wake asyncio.Event.wait() reliably.
            self._not_empty.clear()
            with contextlib.suppress(TimeoutError):
                await _asyncio.wait_for(self._not_empty.wait(), timeout=0.1)

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._core.close()
        self._not_empty.set()
        self._not_full.set()
        # Remove fd readers
        if self._loop is not None:
            if self._data_rd_fd >= 0:
                self._loop.remove_reader(self._data_rd_fd)
            if self._space_rd_fd >= 0:
                self._loop.remove_reader(self._space_rd_fd)

    def cleanup(self) -> None:
        """Remove the shared memory file. Only call from the creator."""
        self._core.cleanup()

    @property
    def closed(self) -> bool:
        return bool(self._core.closed)

    @property
    def stats(self) -> dict:
        return dict(self._core.stats())
