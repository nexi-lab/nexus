"""Cross-process DT_STREAM via shared memory (mmap) — SharedMemoryStreamBackend (#1680).

Implements ``StreamBackend`` protocol using ``SharedStreamBufferCore`` (Rust, mmap)
for cross-process append-only log with independent reader cursors.

For in-process use, prefer ``MemoryStreamBackend`` from ``core/stream.py`` (zero syscall).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Self

from nexus.core.stream import (
    StreamClosedError,
    StreamEmptyError,
    StreamFullError,
    _translate_rust_error,
)

if TYPE_CHECKING:
    from nexus_kernel import SharedStreamBufferCore

# RUST_FALLBACK: SharedStreamBufferCore
from nexus._rust_compat import SharedStreamBufferCore as _SharedStreamBufferCore

logger = logging.getLogger(__name__)


class SharedMemoryStreamBackend:
    """Cross-process append-only log buffer. Implements StreamBackend protocol.

    Uses mmap'd shared memory + OS pipe for writer→reader notification.
    """

    __slots__ = (
        "_core",
        "_data_rd_fd",
        "_loop",
        "_not_empty",
        "_not_full",
    )

    def __init__(
        self,
        core: SharedStreamBufferCore,
        data_rd_fd: int = -1,
    ) -> None:
        self._core = core
        self._data_rd_fd = data_rd_fd
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        if self._loop is not None and self._data_rd_fd >= 0:
            self._loop.add_reader(self._data_rd_fd, self._on_data_available)

    @classmethod
    def create(cls, capacity: int = 65_536) -> tuple[Self, str, int]:
        """Create a new shared stream buffer (writer side).

        Returns:
            (buffer, shm_path, data_rd_fd)
            - shm_path: pass to reader for attach()
            - data_rd_fd: pass to reader (reader listens for data notifications)
        """
        if _SharedStreamBufferCore is None:
            raise RuntimeError("SharedStream requires the nexus-kernel Rust extension.")
        core, shm_path, data_rd_fd = _SharedStreamBufferCore.create(capacity)
        buf = cls(core, data_rd_fd=-1)
        return buf, shm_path, data_rd_fd

    @classmethod
    def attach(cls, shm_path: str, notify_data_wr: int, data_rd_fd: int = -1) -> Self:
        """Attach to an existing shared stream buffer (reader side).

        Args:
            shm_path: Path to the shared memory file.
            notify_data_wr: Write-end of data pipe (passed to Rust core for notification).
            data_rd_fd: Read-end of data pipe (reader listens here).
        """
        if _SharedStreamBufferCore is None:
            raise RuntimeError("SharedStream requires the nexus-kernel Rust extension.")
        core = _SharedStreamBufferCore.attach(shm_path, notify_data_wr)
        return cls(core, data_rd_fd=data_rd_fd)

    # -- fd callback ----------------------------------------------------------

    def _on_data_available(self) -> None:
        with contextlib.suppress(OSError):
            os.read(self._data_rd_fd, 256)
        self._not_empty.set()

    # -- write ----------------------------------------------------------------

    def write_nowait(self, data: bytes) -> int:
        try:
            offset = int(self._core.push(data))
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise
        except ValueError:
            raise
        return offset

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
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

    # -- read (non-destructive, offset-based) ---------------------------------

    def read_at(self, byte_offset: int = 0) -> tuple[bytes, int]:
        try:
            data, next_offset = self._core.read_at(byte_offset)
            return bytes(data), next_offset
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise

    def read_batch(self, byte_offset: int = 0, count: int = 10) -> tuple[list[bytes], int]:
        try:
            items, next_offset = self._core.read_batch(byte_offset, count)
            return [bytes(b) for b in items], next_offset
        except RuntimeError as exc:
            _translate_rust_error(exc)
            raise

    async def read(self, byte_offset: int = 0, *, blocking: bool = True) -> tuple[bytes, int]:
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
                try:
                    return self.read_at(byte_offset)
                except StreamEmptyError:
                    pass
                await self._not_empty.wait()

    async def read_batch_blocking(
        self, byte_offset: int = 0, count: int = 10, *, blocking: bool = True
    ) -> tuple[list[bytes], int]:
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

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._core.close()
        self._not_empty.set()
        self._not_full.set()
        if self._loop is not None and self._data_rd_fd >= 0:
            self._loop.remove_reader(self._data_rd_fd)

    def cleanup(self) -> None:
        self._core.cleanup()

    @property
    def closed(self) -> bool:
        return bool(self._core.closed)

    @property
    def stats(self) -> dict:
        return dict(self._core.stats())

    @property
    def tail(self) -> int:
        return int(self._core.tail)
