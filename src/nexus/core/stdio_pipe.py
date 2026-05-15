"""StdioPipeBackend — PipeBackend over OS subprocess pipes.

Wraps ``asyncio.StreamReader`` / ``asyncio.StreamWriter`` (from
``asyncio.create_subprocess_exec(stdin=PIPE, stdout=PIPE)``) as a
``PipeBackend`` so unmodified 3rd-party CLIs can communicate via
the DT_PIPE kernel primitive.

Newline-framed: each ``write()`` appends ``\\n``, each ``read()``
returns one line (matching JSON-lines ACP/IPC protocol).

    stdio_pipe.py = PipeBackend adapter for OS pipes
    pipe.py       = MemoryPipeBackend (kfifo, in-process, ~0.5μs)
    Rust SharedMemoryPipeBackend = mmap ring buffer (cross-process, ~1-5μs)

See: core/pipe.py for PipeBackend protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from nexus.core.pipe import PipeClosedError, PipeEmptyError

logger = logging.getLogger(__name__)


class StdioPipeBackend:
    """PipeBackend wrapping subprocess OS pipes (asyncio StreamReader/Writer).

    For UNMANAGED agents (3rd-party CLIs) that speak raw stdio.
    Newline-framed: each write appends ``\\n``, each read returns one line.
    """

    __slots__ = ("_reader", "_writer", "_closed", "_write_count", "_read_count")

    def __init__(
        self,
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._write_count = 0
        self._read_count = 0

    # -- PipeBackend protocol -------------------------------------------------

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Write data to the pipe (appends newline).

        When *blocking* is True, drains the writer (waits for OS buffer flush).
        When False, enqueues without drain (fire-and-forget).
        """
        if self._closed:
            raise PipeClosedError("write to closed stdio pipe")
        if self._writer is None:
            raise PipeClosedError("no writer (read-only pipe)")
        payload = data if data.endswith(b"\n") else data + b"\n"
        self._writer.write(payload)
        if blocking:
            await self._writer.drain()
        self._write_count += 1
        return len(payload)

    async def read(self, *, blocking: bool = True) -> bytes:
        """Read one newline-delimited line from the pipe.

        When *blocking* is False, raises ``PipeEmptyError`` if no
        complete line is immediately available.
        """
        if self._reader is None:
            raise PipeClosedError("no reader (write-only pipe)")
        if not blocking:
            if self._reader.at_eof():
                raise PipeClosedError("EOF on stdio pipe")
            raise PipeEmptyError("use async read(blocking=True) for stdio pipes")
        line = await self._reader.readline()
        if not line:
            self._closed = True
            raise PipeClosedError("EOF on stdio pipe")
        self._read_count += 1
        return line

    def write_nowait(self, data: bytes) -> int:
        """Non-blocking write (no drain — best effort)."""
        if self._closed:
            raise PipeClosedError("write to closed stdio pipe")
        if self._writer is None:
            raise PipeClosedError("no writer (read-only pipe)")
        payload = data if data.endswith(b"\n") else data + b"\n"
        self._writer.write(payload)
        self._write_count += 1
        return len(payload)

    def read_nowait(self) -> bytes:
        """Non-blocking read — raises PipeEmptyError (OS pipes can't readline sync)."""
        raise PipeEmptyError("use async read() for stdio pipes")

    async def wait_writable(self) -> None:
        """OS pipe kernel buffer handles backpressure — returns immediately."""

    async def wait_readable(self) -> None:
        """Wait until data is available on the reader."""
        if self._reader is None:
            raise PipeClosedError("no reader (write-only pipe)")
        # Peek by waiting for at least 1 byte without consuming
        await self._reader.read(0)

    def close(self) -> None:
        """Close the writer side of the pipe."""
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> dict:
        return {
            "backend": "stdio",
            "write_count": self._write_count,
            "read_count": self._read_count,
            "closed": self._closed,
        }
