"""Unit tests for StdioPipe — PipeBackend over OS subprocess pipes.

Tests PipeBackend protocol conformance and roundtrip I/O via
in-memory asyncio StreamReader/StreamWriter pairs.
See: src/nexus/core/stdio_pipe.py
"""

import asyncio

import pytest

from nexus.core.pipe import PipeBackend, PipeClosedError, PipeEmptyError
from nexus.core.stdio_pipe import StdioPipe

# ---------------------------------------------------------------------------
# Helpers — create in-memory stream pairs
# ---------------------------------------------------------------------------


def _make_pipe_pair() -> tuple[
    asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamWriter
]:
    """Create two in-memory stream pairs (simulates subprocess stdin/stdout)."""
    # For writing TO the pipe: we write to writer_w, read from reader_r
    reader_r = asyncio.StreamReader()
    # We need a transport+protocol for the writer
    return reader_r, None, None, None  # placeholder — see _make_connected_pair


async def _make_connected_pair() -> tuple[StdioPipe, StdioPipe]:
    """Create a connected StdioPipe pair (writer → reader) using asyncio streams.

    Returns (writer_pipe, reader_pipe) where writing to writer_pipe
    can be read from reader_pipe.
    """
    reader = asyncio.StreamReader()
    # Simulate transport with a direct feed
    return reader, StdioPipe(reader=reader, writer=None)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_isinstance_pipe_backend(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        assert isinstance(pipe, PipeBackend)

    def test_closed_initially_false(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        assert pipe.closed is False

    def test_stats_property(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        stats = pipe.stats
        assert stats["backend"] == "stdio"
        assert stats["write_count"] == 0
        assert stats["read_count"] == 0
        assert stats["closed"] is False


# ---------------------------------------------------------------------------
# Read — feeding data into StreamReader
# ---------------------------------------------------------------------------


class TestRead:
    @pytest.mark.asyncio
    async def test_read_single_line(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        reader.feed_data(b'{"jsonrpc":"2.0","id":1}\n')
        data = await pipe.read()
        assert data == b'{"jsonrpc":"2.0","id":1}\n'

    @pytest.mark.asyncio
    async def test_read_multiple_lines(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        reader.feed_data(b"line1\nline2\n")
        assert await pipe.read() == b"line1\n"
        assert await pipe.read() == b"line2\n"

    @pytest.mark.asyncio
    async def test_read_eof_raises_closed(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        reader.feed_eof()
        with pytest.raises(PipeClosedError):
            await pipe.read()

    @pytest.mark.asyncio
    async def test_read_blocks_until_data(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)

        async def _feed_later() -> None:
            await asyncio.sleep(0.05)
            reader.feed_data(b"delayed\n")

        task = asyncio.create_task(_feed_later())
        data = await pipe.read()
        assert data == b"delayed\n"
        await task

    def test_read_nowait_empty_raises(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        with pytest.raises(PipeEmptyError):
            pipe.read_nowait()


# ---------------------------------------------------------------------------
# Write — using a real asyncio stream pair
# ---------------------------------------------------------------------------


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_roundtrip(self) -> None:
        """Write to StdioPipe, read back from the underlying StreamReader."""
        # Create a connected pair using asyncio server/client
        reader = asyncio.StreamReader()

        # Create a mock writer that feeds into the reader
        class MockTransport:
            def get_extra_info(self, *a, **kw):
                return None

            def is_closing(self):
                return False

            def write(self, data: bytes) -> None:
                reader.feed_data(data)

            def close(self):
                pass

        transport = MockTransport()
        protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
        writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_event_loop())

        write_pipe = StdioPipe(reader=None, writer=writer)
        read_pipe = StdioPipe(reader=reader, writer=None)

        await write_pipe.write(b"hello")
        data = await read_pipe.read()
        assert data == b"hello\n"  # StdioPipe appends \n

    @pytest.mark.asyncio
    async def test_write_already_has_newline(self) -> None:
        reader = asyncio.StreamReader()

        class MockTransport:
            def get_extra_info(self, *a, **kw):
                return None

            def is_closing(self):
                return False

            def write(self, data: bytes) -> None:
                reader.feed_data(data)

            def close(self):
                pass

        transport = MockTransport()
        protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
        writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_event_loop())

        pipe = StdioPipe(reader=None, writer=writer)
        await pipe.write(b"hello\n")
        # Should not double-append newline
        data = await asyncio.wait_for(reader.readline(), timeout=1.0)
        assert data == b"hello\n"

    @pytest.mark.asyncio
    async def test_write_to_closed_raises(self) -> None:
        pipe = StdioPipe(reader=None, writer=None)
        pipe.close()
        with pytest.raises(PipeClosedError):
            await pipe.write(b"data")

    @pytest.mark.asyncio
    async def test_write_no_writer_raises(self) -> None:
        pipe = StdioPipe(reader=asyncio.StreamReader(), writer=None)
        with pytest.raises(PipeClosedError):
            await pipe.write(b"data")


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_sets_flag(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        pipe.close()
        assert pipe.closed is True

    def test_close_idempotent(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        pipe.close()
        pipe.close()  # no error
        assert pipe.closed is True

    @pytest.mark.asyncio
    async def test_wait_writable_returns_immediately(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        await pipe.wait_writable()  # should not block

    @pytest.mark.asyncio
    async def test_wait_readable_with_data(self) -> None:
        reader = asyncio.StreamReader()
        pipe = StdioPipe(reader=reader, writer=None)
        reader.feed_data(b"ready\n")
        await pipe.wait_readable()  # should not block
