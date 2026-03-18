"""StdioStream — StreamBackend over OS subprocess pipes.

Wraps ``asyncio.StreamReader`` as a ``StreamBackend`` with internal
accumulation buffer.  Since OS pipes are consumed-on-read, a pump task
reads lines and appends to an internal buffer, enabling offset-based
multi-reader access.

    stdio_stream.py = StreamBackend adapter for OS pipes
    stream.py       = StreamBuffer (Rust linear buffer, ~0.5μs)
    shm_stream.py   = SharedStreamBuffer (mmap, cross-process, ~1-5μs)

See: core/stream.py for StreamBackend protocol.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import logging

from nexus.core.stream import StreamClosedError, StreamEmptyError

logger = logging.getLogger(__name__)


class StdioStream:
    """StreamBackend wrapping subprocess stdout with internal accumulation.

    A pump task reads lines from the OS pipe and appends to an internal
    buffer.  Multiple readers can ``read_at(offset)`` independently.
    """

    __slots__ = (
        "_reader",
        "_writer",
        "_closed",
        "_buffer",
        "_byte_offsets",
        "_total_bytes",
        "_data_available",
        "_pump_task",
    )

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._buffer: list[bytes] = []
        self._byte_offsets: list[int] = []  # start offset of each message
        self._total_bytes = 0
        self._data_available = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None

    # -- Pump — reads from OS pipe into internal buffer -----------------------

    async def start_pump(self) -> None:
        """Start the background pump task that reads from the OS pipe."""
        self._pump_task = asyncio.create_task(self._pump_loop(), name="stdio-stream-pump")

    async def _pump_loop(self) -> None:
        """Read lines from the OS pipe and accumulate into the buffer."""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF
                self._byte_offsets.append(self._total_bytes)
                self._buffer.append(line)
                self._total_bytes += len(line)
                self._data_available.set()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("StdioStream pump error: %s", exc)
        finally:
            self._closed = True
            self._data_available.set()  # wake blocked readers

    # -- StreamBackend protocol -----------------------------------------------

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Write to stdin of the subprocess (if writer provided).

        When *blocking* is True, drains the writer (waits for OS buffer flush).
        When False, enqueues without drain.
        """
        if self._closed:
            raise StreamClosedError("write to closed stdio stream")
        if self._writer is None:
            raise StreamClosedError("no writer (read-only stream)")
        payload = data if data.endswith(b"\n") else data + b"\n"
        self._writer.write(payload)
        if blocking:
            await self._writer.drain()
        return len(payload)

    def write_nowait(self, data: bytes) -> int:
        """Non-blocking write to stdin."""
        if self._closed:
            raise StreamClosedError("write to closed stdio stream")
        if self._writer is None:
            raise StreamClosedError("no writer (read-only stream)")
        payload = data if data.endswith(b"\n") else data + b"\n"
        self._writer.write(payload)
        return len(payload)

    def read_at(self, byte_offset: int = 0) -> tuple[bytes, int]:
        """Read one message at byte_offset. Returns (data, next_offset).

        Non-destructive — the same offset can be re-read by any reader.
        """
        if not self._buffer:
            if self._closed:
                raise StreamClosedError(f"stream closed, no data at offset {byte_offset}")
            raise StreamEmptyError(f"no data at offset {byte_offset}")

        # Binary search for the message index at this byte offset
        idx = bisect.bisect_right(self._byte_offsets, byte_offset) - 1
        if idx < 0:
            idx = 0
        # If offset is exactly at a message boundary, use that message
        if idx < len(self._byte_offsets) and self._byte_offsets[idx] == byte_offset:
            pass
        elif byte_offset >= self._total_bytes:
            if self._closed:
                raise StreamClosedError(f"stream closed, no data at offset {byte_offset}")
            raise StreamEmptyError(f"no data at offset {byte_offset}")
        else:
            # offset is within the buffer but not at a boundary — find next
            idx = bisect.bisect_right(self._byte_offsets, byte_offset)
            if idx >= len(self._buffer):
                if self._closed:
                    raise StreamClosedError(f"stream closed, no data at offset {byte_offset}")
                raise StreamEmptyError(f"no data at offset {byte_offset}")

        data = self._buffer[idx]
        next_offset = self._byte_offsets[idx] + len(data)
        return data, next_offset

    async def read(self, byte_offset: int = 0, *, blocking: bool = True) -> tuple[bytes, int]:
        """Async read one message at byte_offset. Blocks until data available."""
        while True:
            try:
                return self.read_at(byte_offset)
            except StreamEmptyError:
                if not blocking:
                    raise
                if self._closed:
                    raise StreamClosedError(
                        f"stream closed, no data at offset {byte_offset}"
                    ) from None
                self._data_available.clear()
                # Re-check before sleeping (lost-wakeup race)
                try:
                    return self.read_at(byte_offset)
                except StreamEmptyError:
                    pass
                await self._data_available.wait()

    def read_batch(self, byte_offset: int = 0, count: int = 10) -> tuple[list[bytes], int]:
        """Read up to ``count`` messages starting at byte_offset."""
        if not self._buffer:
            if self._closed:
                raise StreamClosedError(f"stream closed, no data at offset {byte_offset}")
            raise StreamEmptyError(f"no data at offset {byte_offset}")

        # Find start index
        idx = bisect.bisect_left(self._byte_offsets, byte_offset)
        if idx >= len(self._buffer):
            if self._closed:
                raise StreamClosedError(f"stream closed, no data at offset {byte_offset}")
            raise StreamEmptyError(f"no data at offset {byte_offset}")

        end_idx = min(idx + count, len(self._buffer))
        items = self._buffer[idx:end_idx]
        next_offset = self._byte_offsets[end_idx - 1] + len(items[-1]) if items else byte_offset
        return items, next_offset

    async def read_batch_blocking(
        self, byte_offset: int = 0, count: int = 10, *, blocking: bool = True
    ) -> tuple[list[bytes], int]:
        """Async read up to ``count`` messages. Blocks until at least one available."""
        while True:
            try:
                return self.read_batch(byte_offset, count)
            except StreamEmptyError:
                if not blocking:
                    raise
                if self._closed:
                    raise StreamClosedError(
                        f"stream closed, no data at offset {byte_offset}"
                    ) from None
                self._data_available.clear()
                try:
                    return self.read_batch(byte_offset, count)
                except StreamEmptyError:
                    pass
                await self._data_available.wait()

    def close(self) -> None:
        """Close the stream and cancel the pump task."""
        if self._closed:
            return
        self._closed = True
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        self._data_available.set()  # wake blocked readers

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> dict:
        return {
            "backend": "stdio_stream",
            "msg_count": len(self._buffer),
            "total_bytes": self._total_bytes,
            "closed": self._closed,
        }

    @property
    def tail(self) -> int:
        """Current write position (monotonic byte offset)."""
        return self._total_bytes
