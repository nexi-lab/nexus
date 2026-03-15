"""Tests for DT_STREAM — append-only log with offset-based non-destructive reads.

Tests StreamBuffer (kstream) and StreamManager (mkstream).
"""

import pytest

from nexus.core.stream import (
    StreamBuffer,
    StreamClosedError,
    StreamEmptyError,
    StreamFullError,
)

try:
    from nexus_fast import StreamBufferCore  # noqa: F401

    _HAS_STREAM_CORE = True
except ImportError:
    _HAS_STREAM_CORE = False

pytestmark = pytest.mark.skipif(
    not _HAS_STREAM_CORE,
    reason="nexus_fast.StreamBufferCore not built — rebuild Rust extension",
)

# ---------------------------------------------------------------------------
# StreamBuffer (kstream) — basic operations
# ---------------------------------------------------------------------------


class TestStreamBufferBasic:
    """Write / read_at roundtrip, ordering, stats."""

    def test_write_read_roundtrip(self):
        buf = StreamBuffer(capacity=1024)
        offset = buf.write_nowait(b"hello")
        data, next_offset = buf.read_at(offset)
        assert data == b"hello"
        assert next_offset > offset

    def test_write_multiple_read_in_order(self):
        buf = StreamBuffer(capacity=1024)
        o1 = buf.write_nowait(b"aaa")
        o2 = buf.write_nowait(b"bbb")
        o3 = buf.write_nowait(b"ccc")

        d1, n1 = buf.read_at(o1)
        d2, n2 = buf.read_at(n1)
        d3, _ = buf.read_at(n2)

        assert d1 == b"aaa"
        assert d2 == b"bbb"
        assert d3 == b"ccc"
        assert o1 < o2 < o3

    def test_stats(self):
        buf = StreamBuffer(capacity=1024)
        buf.write_nowait(b"x")
        buf.write_nowait(b"y")
        s = buf.stats
        assert s["msg_count"] == 2
        assert s["push_count"] == 2

    def test_tail_monotonic(self):
        buf = StreamBuffer(capacity=1024)
        assert buf.tail == 0
        buf.write_nowait(b"data")
        t1 = buf.tail
        buf.write_nowait(b"more")
        t2 = buf.tail
        assert t2 > t1 > 0


# ---------------------------------------------------------------------------
# StreamBuffer — replay / multi-reader
# ---------------------------------------------------------------------------


class TestStreamBufferReplay:
    """read_at(0) replays from start, same offset re-readable."""

    def test_replay_from_start(self):
        buf = StreamBuffer(capacity=1024)
        buf.write_nowait(b"first")
        buf.write_nowait(b"second")

        # Read from 0 twice — same result (non-destructive)
        d1, n1 = buf.read_at(0)
        d2, n2 = buf.read_at(0)
        assert d1 == d2 == b"first"
        assert n1 == n2

    def test_same_offset_re_readable(self):
        buf = StreamBuffer(capacity=1024)
        offset = buf.write_nowait(b"hello")
        for _ in range(5):
            data, _ = buf.read_at(offset)
            assert data == b"hello"


class TestStreamBufferMultiReader:
    """Two readers at different offsets."""

    def test_independent_cursors(self):
        buf = StreamBuffer(capacity=1024)
        buf.write_nowait(b"msg1")
        buf.write_nowait(b"msg2")
        buf.write_nowait(b"msg3")

        # Reader A reads from start
        da, na = buf.read_at(0)
        assert da == b"msg1"

        # Reader B reads further ahead
        _, nb = buf.read_at(na)
        db, _ = buf.read_at(nb)
        assert db == b"msg3"

        # Reader A still at msg1 offset — can re-read
        da2, _ = buf.read_at(0)
        assert da2 == b"msg1"


# ---------------------------------------------------------------------------
# StreamBuffer — batch reads
# ---------------------------------------------------------------------------


class TestStreamBufferBatch:
    """read_batch with various counts."""

    def test_batch_all(self):
        buf = StreamBuffer(capacity=1024)
        buf.write_nowait(b"a")
        buf.write_nowait(b"b")
        buf.write_nowait(b"c")

        items, next_off = buf.read_batch(0, count=10)
        assert len(items) == 3
        assert items == [b"a", b"b", b"c"]

    def test_batch_partial(self):
        buf = StreamBuffer(capacity=1024)
        buf.write_nowait(b"a")
        buf.write_nowait(b"b")
        buf.write_nowait(b"c")

        items, next_off = buf.read_batch(0, count=2)
        assert len(items) == 2
        assert items == [b"a", b"b"]

        # Continue from next_off
        items2, _ = buf.read_batch(next_off, count=10)
        assert items2 == [b"c"]

    def test_batch_count_one(self):
        buf = StreamBuffer(capacity=1024)
        buf.write_nowait(b"only")

        items, _ = buf.read_batch(0, count=1)
        assert items == [b"only"]


# ---------------------------------------------------------------------------
# StreamBuffer — capacity and errors
# ---------------------------------------------------------------------------


class TestStreamBufferCapacity:
    """Oversized, exact capacity, full error."""

    def test_oversized_message(self):
        buf = StreamBuffer(capacity=64)
        with pytest.raises(ValueError):
            buf.write_nowait(b"x" * 1000)

    def test_full_error(self):
        buf = StreamBuffer(capacity=64)
        # Fill the buffer
        while True:
            try:
                buf.write_nowait(b"xxxx")
            except (StreamFullError, ValueError):
                break

    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            StreamBuffer(capacity=0)
        with pytest.raises(ValueError):
            StreamBuffer(capacity=-1)

    def test_empty_read(self):
        buf = StreamBuffer(capacity=1024)
        with pytest.raises(StreamEmptyError):
            buf.read_at(0)


# ---------------------------------------------------------------------------
# StreamBuffer — close semantics
# ---------------------------------------------------------------------------


class TestStreamBufferClose:
    """Write after close, close semantics."""

    def test_write_after_close(self):
        buf = StreamBuffer(capacity=1024)
        buf.write_nowait(b"before")
        buf.close()
        with pytest.raises(StreamClosedError):
            buf.write_nowait(b"after")

    def test_read_existing_after_close(self):
        buf = StreamBuffer(capacity=1024)
        offset = buf.write_nowait(b"data")
        buf.close()
        # Existing data still readable after close
        data, _ = buf.read_at(offset)
        assert data == b"data"

    def test_closed_property(self):
        buf = StreamBuffer(capacity=1024)
        assert not buf.closed
        buf.close()
        assert buf.closed


# ---------------------------------------------------------------------------
# StreamManager (mkstream) — lifecycle
# ---------------------------------------------------------------------------


class TestStreamManager:
    """create, open, destroy, list, close_all."""

    @pytest.fixture()
    def manager(self, tmp_path):
        from unittest.mock import MagicMock

        from nexus.contracts.metadata import FileMetadata

        # Simple in-memory metastore mock
        store: dict[str, FileMetadata] = {}

        mock = MagicMock()
        mock.get = lambda p: store.get(p)
        mock.put = lambda m: store.__setitem__(m.path, m)
        mock.delete = lambda p: store.pop(p, None)

        from nexus.core.stream_manager import StreamManager

        return StreamManager(mock, zone_id="root", self_address=None)

    def test_create_and_read(self, manager):
        buf = manager.create("/streams/test", capacity=1024)
        offset = buf.write_nowait(b"hello")

        data, _ = manager.stream_read_at("/streams/test", offset)
        assert data == b"hello"

    def test_create_duplicate_raises(self, manager):
        manager.create("/streams/test")
        from nexus.core.stream import StreamError

        with pytest.raises(StreamError, match="already exists"):
            manager.create("/streams/test")

    def test_open_existing(self, manager):
        manager.create("/streams/test")
        buf = manager.open("/streams/test")
        assert not buf.closed

    def test_open_not_found(self, manager):
        from nexus.core.stream import StreamNotFoundError

        with pytest.raises(StreamNotFoundError):
            manager.open("/streams/nonexistent")

    def test_destroy(self, manager):
        manager.create("/streams/test")
        manager.destroy("/streams/test")

        from nexus.core.stream import StreamNotFoundError

        with pytest.raises(StreamNotFoundError):
            manager.stream_read_at("/streams/test", 0)

    def test_list_streams(self, manager):
        manager.create("/streams/a")
        manager.create("/streams/b")

        streams = manager.list_streams()
        assert "/streams/a" in streams
        assert "/streams/b" in streams

    def test_close_all(self, manager):
        buf_a = manager.create("/streams/a")
        buf_b = manager.create("/streams/b")

        manager.close_all()
        assert buf_a.closed
        assert buf_b.closed

    def test_signal_close(self, manager):
        buf = manager.create("/streams/test")
        manager.signal_close("/streams/test")
        assert buf.closed
        # Buffer still in registry — can still read existing data

    def test_write_nowait(self, manager):
        manager.create("/streams/test")
        offset = manager.stream_write_nowait("/streams/test", b"sync_data")
        data, _ = manager.stream_read_at("/streams/test", offset)
        assert data == b"sync_data"


class TestStreamManagerFederation:
    """self_address and backend_name encoding."""

    def test_no_self_address(self, tmp_path):
        from unittest.mock import MagicMock

        store: dict = {}
        mock = MagicMock()
        mock.get = lambda p: store.get(p)
        mock.put = lambda m: store.__setitem__(m.path, m)

        from nexus.core.stream_manager import StreamManager

        mgr = StreamManager(mock, self_address=None)
        mgr.create("/s/test")

        meta = store["/s/test"]
        assert meta.backend_name == "stream"

    def test_with_self_address(self, tmp_path):
        from unittest.mock import MagicMock

        store: dict = {}
        mock = MagicMock()
        mock.get = lambda p: store.get(p)
        mock.put = lambda m: store.__setitem__(m.path, m)

        from nexus.core.stream_manager import StreamManager

        mgr = StreamManager(mock, self_address="node1:5050")
        mgr.create("/s/test")

        meta = store["/s/test"]
        assert meta.backend_name == "stream@node1:5050"
