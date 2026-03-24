"""Unit tests for SharedRingBuffer — cross-process SPSC ring buffer via mmap (#1680).

Same-process tests (create + attach in same process, valid because MAP_SHARED).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("nexus_fast")

from nexus_fast import SharedRingBufferCore

from nexus.core.pipe import PipeBackend, PipeEmptyError, PipeFullError
from nexus.core.shm_pipe import SharedRingBuffer

# ---------------------------------------------------------------------------
# Rust core tests (bypass Python wrapper)
# ---------------------------------------------------------------------------


class TestSharedRingBufferCore:
    """Tests for the Rust SharedRingBufferCore directly."""

    def test_create_returns_handles(self):
        core, shm_path, data_rd_fd, space_rd_fd = SharedRingBufferCore.create(1024)
        assert shm_path
        assert data_rd_fd >= 0
        assert space_rd_fd >= 0
        import os

        os.close(data_rd_fd)
        os.close(space_rd_fd)
        core.cleanup()

    def test_write_read_roundtrip(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(1024)
        reader = SharedRingBufferCore.attach(shm_path, -1, -1)
        import os

        os.close(dfd)
        os.close(sfd)

        core.push(b"hello")
        result = reader.pop()
        assert result == b"hello"
        core.cleanup()

    def test_fifo_ordering(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(1024)
        reader = SharedRingBufferCore.attach(shm_path, -1, -1)
        import os

        os.close(dfd)
        os.close(sfd)

        core.push(b"first")
        core.push(b"second")
        assert reader.pop() == b"first"
        assert reader.pop() == b"second"
        core.cleanup()

    def test_close_propagates(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(1024)
        reader = SharedRingBufferCore.attach(shm_path, -1, -1)
        import os

        os.close(dfd)
        os.close(sfd)

        assert not reader.closed
        core.close()
        assert reader.closed
        core.cleanup()

    def test_cleanup_removes_file(self):
        import os

        core, shm_path, dfd, sfd = SharedRingBufferCore.create(64)
        os.close(dfd)
        os.close(sfd)
        assert os.path.exists(shm_path)
        core.cleanup()
        assert not os.path.exists(shm_path)

    def test_wrap_around(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(64)
        reader = SharedRingBufferCore.attach(shm_path, -1, -1)
        import os

        os.close(dfd)
        os.close(sfd)

        for i in range(20):
            data = bytes([i]) * 50
            core.push(data)
            result = reader.pop()
            assert result == data

        core.cleanup()

    def test_u64_roundtrip(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(1024)
        reader = SharedRingBufferCore.attach(shm_path, -1, -1)
        import os

        os.close(dfd)
        os.close(sfd)

        core.push_u64(42)
        core.push_u64(2**64 - 1)
        assert reader.pop_u64() == 42
        assert reader.pop_u64() == 2**64 - 1
        core.cleanup()

    def test_stats(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(1024)
        import os

        os.close(dfd)
        os.close(sfd)

        core.push(b"hello")
        stats = core.stats()
        assert stats["size"] == 5
        assert stats["capacity"] == 1024
        assert stats["msg_count"] == 1
        assert stats["push_count"] == 1
        assert not stats["closed"]
        core.cleanup()

    def test_empty_error(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(64)
        import os

        os.close(dfd)
        os.close(sfd)

        with pytest.raises(RuntimeError, match="PipeEmpty"):
            core.pop()
        core.cleanup()

    def test_full_error(self):
        core, shm_path, dfd, sfd = SharedRingBufferCore.create(10)
        import os

        os.close(dfd)
        os.close(sfd)

        core.push(b"x" * 10)
        with pytest.raises(RuntimeError, match="PipeFull"):
            core.push(b"y")
        core.cleanup()


# ---------------------------------------------------------------------------
# Python wrapper tests
# ---------------------------------------------------------------------------


class TestSharedRingBuffer:
    """Tests for the Python SharedRingBuffer wrapper."""

    def _create_pair(self, capacity=1024):
        """Create a writer + reader pair in the same process."""
        core_w, shm_path, dfd, sfd = SharedRingBufferCore.create(capacity)
        core_r = SharedRingBufferCore.attach(shm_path, -1, -1)
        import os

        os.close(dfd)
        os.close(sfd)
        # Wrap in Python class without fd-based notification (same process)
        writer = SharedRingBuffer(core_w)
        reader = SharedRingBuffer(core_r)
        return writer, reader, shm_path

    def test_protocol_conformance(self):
        """SharedRingBuffer satisfies PipeBackend protocol."""
        writer, reader, _ = self._create_pair()
        assert isinstance(writer, PipeBackend)
        assert isinstance(reader, PipeBackend)
        writer.close()
        reader.close()

    def test_write_read_nowait(self):
        writer, reader, _ = self._create_pair()
        writer.write_nowait(b"hello")
        # Manually signal since no fd notification in same-process
        reader._not_empty.set()
        result = reader.read_nowait()
        assert result == b"hello"
        writer.close()

    @pytest.mark.asyncio
    async def test_async_write_read(self):
        writer, reader, _ = self._create_pair()

        async def produce():
            await writer.write(b"async-msg")

        async def consume():
            # Small delay to let producer run
            await asyncio.sleep(0.01)
            reader._not_empty.set()  # manual signal in same-process
            return await reader.read()

        _, result = await asyncio.gather(produce(), consume())
        assert result == b"async-msg"
        writer.close()

    def test_close_propagates(self):
        writer, reader, _ = self._create_pair()
        assert not reader.closed
        writer.close()
        assert reader.closed

    def test_stats(self):
        writer, reader, _ = self._create_pair()
        writer.write_nowait(b"test")
        stats = writer.stats
        assert stats["size"] == 4
        assert stats["msg_count"] == 1
        writer.close()

    def test_full_raises(self):
        writer, _, _ = self._create_pair(capacity=10)
        writer.write_nowait(b"x" * 10)
        with pytest.raises(PipeFullError):
            writer.write_nowait(b"y")
        writer.close()

    def test_empty_raises(self):
        _, reader, _ = self._create_pair()
        with pytest.raises(PipeEmptyError):
            reader.read_nowait()
        reader.close()
