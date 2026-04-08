"""Unit tests for SharedMemoryStreamBackend — cross-process append-only log via mmap (#1680).

Same-process tests (create + attach in same process, valid because MAP_SHARED).
"""

from __future__ import annotations

import pytest

pytest.importorskip("nexus_kernel")

from nexus_kernel import SharedMemoryStreamBackend as RustStreamBackend

from nexus.core.shm_stream import SharedMemoryStreamBackend
from nexus.core.stream import StreamBackend, StreamEmptyError

# ---------------------------------------------------------------------------
# Rust core tests (bypass Python wrapper)
# ---------------------------------------------------------------------------


class TestRustStreamBackend:
    """Tests for the Rust SharedMemoryStreamBackend directly."""

    def test_create_returns_handles(self):
        core, shm_path, data_rd_fd = RustStreamBackend.create(1024)
        assert shm_path
        assert data_rd_fd >= 0
        import os

        os.close(data_rd_fd)
        core.cleanup()

    def test_write_read_at_roundtrip(self):
        core, shm_path, dfd = RustStreamBackend.create(1024)
        reader = RustStreamBackend.attach(shm_path, -1)
        import os

        os.close(dfd)

        offset = core.push(b"hello")
        assert offset == 0
        data, next_offset = reader.read_at(0)
        assert data == b"hello"
        assert next_offset == 4 + 5  # HEADER_SIZE + payload
        core.cleanup()

    def test_multi_reader_independent_cursors(self):
        core, shm_path, dfd = RustStreamBackend.create(1024)
        reader = RustStreamBackend.attach(shm_path, -1)
        import os

        os.close(dfd)

        core.push(b"msg1")
        core.push(b"msg2")

        # Reader A reads both
        d1, n1 = reader.read_at(0)
        d2, _ = reader.read_at(n1)
        assert d1 == b"msg1"
        assert d2 == b"msg2"

        # Reader B re-reads from 0 (non-destructive)
        d1b, _ = reader.read_at(0)
        assert d1b == b"msg1"

        core.cleanup()

    def test_tail_monotonic(self):
        core, shm_path, dfd = RustStreamBackend.create(1024)
        import os

        os.close(dfd)

        t0 = core.tail
        core.push(b"a")
        t1 = core.tail
        core.push(b"b")
        t2 = core.tail
        assert t0 < t1 < t2
        core.cleanup()

    def test_close_propagates(self):
        core, shm_path, dfd = RustStreamBackend.create(1024)
        reader = RustStreamBackend.attach(shm_path, -1)
        import os

        os.close(dfd)

        assert not reader.closed
        core.close()
        assert reader.closed
        core.cleanup()

    def test_cleanup_removes_file(self):
        import os

        core, shm_path, dfd = RustStreamBackend.create(64)
        os.close(dfd)
        assert os.path.exists(shm_path)
        core.cleanup()
        assert not os.path.exists(shm_path)

    def test_read_batch(self):
        core, shm_path, dfd = RustStreamBackend.create(1024)
        reader = RustStreamBackend.attach(shm_path, -1)
        import os

        os.close(dfd)

        core.push(b"a")
        core.push(b"b")
        core.push(b"c")

        items, next_offset = reader.read_batch(0, 10)
        assert len(items) == 3
        assert items[0] == b"a"
        assert items[1] == b"b"
        assert items[2] == b"c"
        core.cleanup()

    def test_stats(self):
        core, shm_path, dfd = RustStreamBackend.create(1024)
        import os

        os.close(dfd)

        core.push(b"hello")
        stats = core.stats()
        assert stats["msg_count"] == 1
        assert stats["push_count"] == 1
        assert stats["tail"] == 4 + 5
        assert not stats["closed"]
        core.cleanup()


# ---------------------------------------------------------------------------
# Python wrapper tests
# ---------------------------------------------------------------------------


class TestSharedMemoryStreamBackend:
    """Tests for the Python SharedMemoryStreamBackend wrapper."""

    def _create_pair(self, capacity=1024):
        core_w, shm_path, dfd = RustStreamBackend.create(capacity)
        core_r = RustStreamBackend.attach(shm_path, -1)
        import os

        os.close(dfd)
        writer = SharedMemoryStreamBackend(core_w)
        reader = SharedMemoryStreamBackend(core_r)
        return writer, reader, shm_path

    def test_protocol_conformance(self):
        """SharedMemoryStreamBackend satisfies StreamBackend protocol."""
        writer, reader, _ = self._create_pair()
        assert isinstance(writer, StreamBackend)
        assert isinstance(reader, StreamBackend)
        writer.close()
        reader.close()

    def test_write_read_at(self):
        writer, reader, _ = self._create_pair()
        offset = writer.write_nowait(b"hello")
        assert offset == 0
        data, next_offset = reader.read_at(0)
        assert data == b"hello"
        writer.close()

    def test_read_batch(self):
        writer, reader, _ = self._create_pair()
        writer.write_nowait(b"a")
        writer.write_nowait(b"b")
        items, _ = reader.read_batch(0, 10)
        assert items == [b"a", b"b"]
        writer.close()

    def test_tail_property(self):
        writer, reader, _ = self._create_pair()
        assert writer.tail == 0
        writer.write_nowait(b"x")
        assert writer.tail > 0
        # Reader sees same tail (shared memory)
        assert reader.tail == writer.tail
        writer.close()

    def test_close_propagates(self):
        writer, reader, _ = self._create_pair()
        assert not reader.closed
        writer.close()
        assert reader.closed

    def test_stats(self):
        writer, _, _ = self._create_pair()
        writer.write_nowait(b"test")
        stats = writer.stats
        assert stats["msg_count"] == 1
        writer.close()

    def test_empty_raises(self):
        _, reader, _ = self._create_pair()
        with pytest.raises(StreamEmptyError):
            reader.read_at(0)
        reader.close()
