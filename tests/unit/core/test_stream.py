"""Tests for DT_STREAM — kernel IPC stream operations.

All stream operations go through the Rust PyKernel class.
Covers: write/read roundtrip, replay, multi-reader, batch reads,
capacity limits, close semantics, and stream lifecycle management.
"""

import pytest

try:
    from nexus_runtime import PyKernel

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not RUST_AVAILABLE,
    reason="nexus_runtime not built — rebuild Rust extension",
)


def _make_kernel() -> "PyKernel":
    return PyKernel()


# ---------------------------------------------------------------------------
# PyKernel IPC Stream — basic write / read_at
# ---------------------------------------------------------------------------


class TestKernelStreamBasic:
    """Write / read_at roundtrip, ordering."""

    def test_create_and_has(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        assert k.has_stream("/streams/test")

    def test_has_stream_nonexistent(self):
        k = _make_kernel()
        assert not k.has_stream("/streams/nope")

    def test_write_read_roundtrip(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        offset = k.stream_write_nowait("/streams/test", b"hello")
        result = k.stream_read_at("/streams/test", offset)
        assert result is not None
        data, next_offset = result
        assert data == b"hello"
        assert next_offset > offset

    def test_write_multiple_read_in_order(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        o1 = k.stream_write_nowait("/streams/test", b"aaa")
        o2 = k.stream_write_nowait("/streams/test", b"bbb")
        o3 = k.stream_write_nowait("/streams/test", b"ccc")

        r1 = k.stream_read_at("/streams/test", o1)
        assert r1 is not None
        d1, n1 = r1

        r2 = k.stream_read_at("/streams/test", n1)
        assert r2 is not None
        d2, n2 = r2

        r3 = k.stream_read_at("/streams/test", n2)
        assert r3 is not None
        d3, _ = r3

        assert d1 == b"aaa"
        assert d2 == b"bbb"
        assert d3 == b"ccc"
        assert o1 < o2 < o3

    def test_read_at_end_returns_none(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        offset = k.stream_write_nowait("/streams/test", b"only")
        result = k.stream_read_at("/streams/test", offset)
        assert result is not None
        _, next_offset = result
        # Reading past the last message returns None
        assert k.stream_read_at("/streams/test", next_offset) is None

    def test_read_empty_stream_returns_none(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        assert k.stream_read_at("/streams/test", 0) is None


# ---------------------------------------------------------------------------
# PyKernel IPC Stream — replay / multi-reader
# ---------------------------------------------------------------------------


class TestKernelStreamReplay:
    """read_at(0) replays from start, same offset re-readable (non-destructive)."""

    def test_replay_from_start(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.stream_write_nowait("/streams/test", b"first")
        k.stream_write_nowait("/streams/test", b"second")

        # Read from 0 twice — same result (non-destructive)
        r1 = k.stream_read_at("/streams/test", 0)
        r2 = k.stream_read_at("/streams/test", 0)
        assert r1 is not None and r2 is not None
        d1, n1 = r1
        d2, n2 = r2
        assert d1 == d2 == b"first"
        assert n1 == n2

    def test_same_offset_re_readable(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        offset = k.stream_write_nowait("/streams/test", b"hello")
        for _ in range(5):
            result = k.stream_read_at("/streams/test", offset)
            assert result is not None
            data, _ = result
            assert data == b"hello"


class TestKernelStreamMultiReader:
    """Two readers at different offsets — independent cursors."""

    def test_independent_cursors(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.stream_write_nowait("/streams/test", b"msg1")
        k.stream_write_nowait("/streams/test", b"msg2")
        k.stream_write_nowait("/streams/test", b"msg3")

        # Reader A reads from start
        ra = k.stream_read_at("/streams/test", 0)
        assert ra is not None
        da, na = ra
        assert da == b"msg1"

        # Reader B reads further ahead
        rb = k.stream_read_at("/streams/test", na)
        assert rb is not None
        _, nb = rb
        rc = k.stream_read_at("/streams/test", nb)
        assert rc is not None
        db, _ = rc
        assert db == b"msg3"

        # Reader A still at msg1 offset — can re-read
        ra2 = k.stream_read_at("/streams/test", 0)
        assert ra2 is not None
        da2, _ = ra2
        assert da2 == b"msg1"


# ---------------------------------------------------------------------------
# PyKernel IPC Stream — batch reads
# ---------------------------------------------------------------------------


class TestKernelStreamBatch:
    """stream_read_batch with various counts."""

    def test_batch_all(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.stream_write_nowait("/streams/test", b"a")
        k.stream_write_nowait("/streams/test", b"b")
        k.stream_write_nowait("/streams/test", b"c")

        items, _next_off = k.stream_read_batch("/streams/test", 0, 10)
        assert len(items) == 3
        assert items == [b"a", b"b", b"c"]

    def test_batch_partial(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.stream_write_nowait("/streams/test", b"a")
        k.stream_write_nowait("/streams/test", b"b")
        k.stream_write_nowait("/streams/test", b"c")

        items, next_off = k.stream_read_batch("/streams/test", 0, 2)
        assert len(items) == 2
        assert items == [b"a", b"b"]

        # Continue from next_off
        items2, _ = k.stream_read_batch("/streams/test", next_off, 10)
        assert items2 == [b"c"]

    def test_batch_count_one(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.stream_write_nowait("/streams/test", b"only")

        items, _ = k.stream_read_batch("/streams/test", 0, 1)
        assert items == [b"only"]

    def test_batch_empty_stream(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)

        items, next_off = k.stream_read_batch("/streams/test", 0, 10)
        assert items == []
        assert next_off == 0


# ---------------------------------------------------------------------------
# PyKernel IPC Stream — capacity and errors
# ---------------------------------------------------------------------------


class TestKernelStreamCapacity:
    """Oversized message, full buffer."""

    def test_oversized_message(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 64)
        with pytest.raises((RuntimeError, ValueError)):
            k.stream_write_nowait("/streams/test", b"x" * 1000)

    def test_full_error(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 64)
        # Fill the buffer until it raises
        with pytest.raises((RuntimeError, ValueError)):
            for _ in range(1000):
                k.stream_write_nowait("/streams/test", b"xxxx")

    def test_zero_capacity_full_immediately(self):
        """Zero capacity buffer rejects all writes immediately."""
        k = _make_kernel()
        k.create_stream("/streams/zero", 0)
        with pytest.raises(RuntimeError):
            k.stream_write_nowait("/streams/zero", b"x")


# ---------------------------------------------------------------------------
# PyKernel IPC Stream — close semantics
# ---------------------------------------------------------------------------


class TestKernelStreamClose:
    """Write after close, read existing data after close."""

    def test_write_after_close_raises(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.stream_write_nowait("/streams/test", b"before")
        k.close_stream("/streams/test")
        with pytest.raises(RuntimeError, match="StreamClosed"):
            k.stream_write_nowait("/streams/test", b"after")

    def test_read_existing_after_close(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        offset = k.stream_write_nowait("/streams/test", b"data")
        k.close_stream("/streams/test")
        # Existing data still readable after close
        result = k.stream_read_at("/streams/test", offset)
        assert result is not None
        data, _ = result
        assert data == b"data"


# ---------------------------------------------------------------------------
# PyKernel IPC Stream — lifecycle management
# ---------------------------------------------------------------------------


class TestKernelStreamLifecycle:
    """create, destroy, list, close_all."""

    def test_create_duplicate_raises(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        with pytest.raises(RuntimeError, match="StreamExists"):
            k.create_stream("/streams/test", 1024)

    def test_destroy_stream(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.destroy_stream("/streams/test")
        assert not k.has_stream("/streams/test")

    def test_destroy_nonexistent_raises(self):
        k = _make_kernel()
        with pytest.raises(FileNotFoundError):
            k.destroy_stream("/streams/nonexistent")

    def test_list_streams(self):
        k = _make_kernel()
        k.create_stream("/streams/a", 1024)
        k.create_stream("/streams/b", 1024)

        streams = k.list_streams()
        assert "/streams/a" in streams
        assert "/streams/b" in streams

    def test_list_streams_empty(self):
        k = _make_kernel()
        assert k.list_streams() == []

    def test_close_all_streams(self):
        k = _make_kernel()
        k.create_stream("/streams/a", 1024)
        k.create_stream("/streams/b", 1024)
        k.stream_write_nowait("/streams/a", b"data_a")
        k.stream_write_nowait("/streams/b", b"data_b")

        k.close_all_streams()

        # Writes should fail after close_all
        with pytest.raises(RuntimeError, match="StreamClosed"):
            k.stream_write_nowait("/streams/a", b"nope")
        with pytest.raises(RuntimeError, match="StreamClosed"):
            k.stream_write_nowait("/streams/b", b"nope")

    def test_close_stream_then_read_remaining(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        offset = k.stream_write_nowait("/streams/test", b"msg1")
        k.stream_write_nowait("/streams/test", b"msg2")
        k.close_stream("/streams/test")

        # Can still read all existing data after close
        items, _ = k.stream_read_batch("/streams/test", offset, 10)
        assert items == [b"msg1", b"msg2"]

    def test_destroy_after_close(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.close_stream("/streams/test")
        k.destroy_stream("/streams/test")
        assert not k.has_stream("/streams/test")

    def test_create_after_destroy_reuses_path(self):
        k = _make_kernel()
        k.create_stream("/streams/test", 1024)
        k.destroy_stream("/streams/test")
        # Should succeed — path is free again
        k.create_stream("/streams/test", 2048)
        assert k.has_stream("/streams/test")
