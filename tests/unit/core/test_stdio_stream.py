"""Unit tests for StdioStreamBackend — StreamBackend over OS subprocess pipes.

Tests StreamBackend protocol conformance, pump task, offset-based
multi-reader access, and batch reads.
See: src/nexus/core/stdio_stream.py
"""

import asyncio

import pytest

from nexus.core.stdio_stream import StdioStreamBackend
from nexus.core.stream import StreamBackend, StreamClosedError, StreamEmptyError

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    @pytest.mark.asyncio
    async def test_isinstance_stream_backend(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        assert isinstance(stream, StreamBackend)

    @pytest.mark.asyncio
    async def test_closed_initially_false(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        assert stream.closed is False

    @pytest.mark.asyncio
    async def test_stats_property(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        stats = stream.stats
        assert stats["backend"] == "stdio_stream"
        assert stats["msg_count"] == 0
        assert stats["total_bytes"] == 0
        assert stats["closed"] is False

    @pytest.mark.asyncio
    async def test_tail_initially_zero(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        assert stream.tail == 0


# ---------------------------------------------------------------------------
# Pump + read_at
# ---------------------------------------------------------------------------


class TestPumpAndRead:
    @pytest.mark.asyncio
    async def test_pump_accumulates_lines(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"line1\nline2\nline3\n")
        reader.feed_eof()

        await stream.start_pump()
        # Wait for pump to finish
        await asyncio.sleep(0.05)

        assert stream.tail > 0
        assert stream.stats["msg_count"] == 3

    @pytest.mark.asyncio
    async def test_read_at_first_message(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"hello\nworld\n")
        reader.feed_eof()

        await stream.start_pump()
        await asyncio.sleep(0.05)

        data, next_offset = stream.read_at(0)
        assert data == b"hello\n"
        assert next_offset == 6  # len("hello\n")

    @pytest.mark.asyncio
    async def test_read_at_second_message(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"hello\nworld\n")
        reader.feed_eof()

        await stream.start_pump()
        await asyncio.sleep(0.05)

        _, next_offset = stream.read_at(0)
        data2, next_offset2 = stream.read_at(next_offset)
        assert data2 == b"world\n"
        assert next_offset2 == 12  # len("hello\n") + len("world\n")

    @pytest.mark.asyncio
    async def test_read_at_empty_raises(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        with pytest.raises(StreamEmptyError):
            stream.read_at(0)

    @pytest.mark.asyncio
    async def test_read_at_past_end_raises(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"only\n")
        reader.feed_eof()

        await stream.start_pump()
        await asyncio.sleep(0.05)

        with pytest.raises(StreamClosedError):
            stream.read_at(999)


# ---------------------------------------------------------------------------
# Multi-reader independent cursors
# ---------------------------------------------------------------------------


class TestMultiReader:
    @pytest.mark.asyncio
    async def test_independent_cursors(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"a\nb\nc\n")
        reader.feed_eof()

        await stream.start_pump()
        await asyncio.sleep(0.05)

        # Reader 1 reads all three
        d1, off1 = stream.read_at(0)
        assert d1 == b"a\n"
        d2, off2 = stream.read_at(off1)
        assert d2 == b"b\n"
        d3, off3 = stream.read_at(off2)
        assert d3 == b"c\n"

        # Reader 2 can re-read from beginning
        d1b, _ = stream.read_at(0)
        assert d1b == b"a\n"

    @pytest.mark.asyncio
    async def test_tail_monotonic(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"msg1\n")
        reader.feed_eof()

        await stream.start_pump()
        await asyncio.sleep(0.05)

        tail1 = stream.tail
        assert tail1 == 5  # len("msg1\n")
        # tail should equal total bytes accumulated
        assert tail1 == stream.stats["total_bytes"]


# ---------------------------------------------------------------------------
# Async read (blocking)
# ---------------------------------------------------------------------------


class TestAsyncRead:
    @pytest.mark.asyncio
    async def test_async_read_blocks_until_data(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        await stream.start_pump()

        async def _feed_later() -> None:
            await asyncio.sleep(0.05)
            reader.feed_data(b"delayed\n")
            reader.feed_eof()

        task = asyncio.create_task(_feed_later())
        data, _ = await stream.read(0)
        assert data == b"delayed\n"
        await task

    @pytest.mark.asyncio
    async def test_async_read_non_blocking(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        with pytest.raises(StreamEmptyError):
            await stream.read(0, blocking=False)


# ---------------------------------------------------------------------------
# Batch reads
# ---------------------------------------------------------------------------


class TestBatchRead:
    @pytest.mark.asyncio
    async def test_read_batch_all(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"a\nb\nc\nd\ne\n")
        reader.feed_eof()

        await stream.start_pump()
        await asyncio.sleep(0.05)

        items, next_off = stream.read_batch(0, count=10)
        assert len(items) == 5
        assert items[0] == b"a\n"
        assert items[4] == b"e\n"

    @pytest.mark.asyncio
    async def test_read_batch_partial(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)

        reader.feed_data(b"a\nb\nc\nd\ne\n")
        reader.feed_eof()

        await stream.start_pump()
        await asyncio.sleep(0.05)

        items, next_off = stream.read_batch(0, count=2)
        assert len(items) == 2
        assert items[0] == b"a\n"
        assert items[1] == b"b\n"

        # Continue from next_off
        items2, _ = stream.read_batch(next_off, count=2)
        assert len(items2) == 2
        assert items2[0] == b"c\n"


# ---------------------------------------------------------------------------
# Write (stdin direction)
# ---------------------------------------------------------------------------


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_no_writer_raises(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader, writer=None)
        with pytest.raises(StreamClosedError):
            await stream.write(b"data")

    @pytest.mark.asyncio
    async def test_write_closed_raises(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        stream.close()
        with pytest.raises(StreamClosedError):
            await stream.write(b"data")


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_sets_flag(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        stream.close()
        assert stream.closed is True

    @pytest.mark.asyncio
    async def test_close_wakes_blocked_readers(self) -> None:
        reader = asyncio.StreamReader()
        stream = StdioStreamBackend(reader)
        await stream.start_pump()

        async def _reader() -> None:
            with pytest.raises(StreamClosedError):
                await stream.read(0)

        task = asyncio.create_task(_reader())
        await asyncio.sleep(0.05)
        stream.close()
        reader.feed_eof()
        await asyncio.wait_for(task, timeout=2.0)
