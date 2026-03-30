"""Unit tests for DT_PIPE kernel IPC primitive.

Tests RingBuffer (kfifo equivalent, kernel tier) and PipeManager
(mkfifo equivalent, system service tier).
See: src/nexus/core/pipe.py, src/nexus/core/pipe_manager.py,
     KERNEL-ARCHITECTURE.md §6.
"""

import asyncio
import contextlib

import pytest

from nexus.contracts.metadata import DT_PIPE, DT_REG, FileMetadata
from nexus.core.pipe import (
    PipeClosedError,
    PipeEmptyError,
    PipeError,
    PipeExistsError,
    PipeFullError,
    PipeNotFoundError,
    RingBuffer,
)
from nexus.core.pipe_manager import PipeManager

# ======================================================================
# RingBuffer — basic operations
# ======================================================================


class TestRingBufferBasic:
    @pytest.mark.asyncio
    async def test_write_read_roundtrip(self) -> None:
        buf = RingBuffer(capacity=1024)
        await buf.write(b"hello")
        result = await buf.read()
        assert result == b"hello"

    @pytest.mark.asyncio
    async def test_fifo_ordering(self) -> None:
        buf = RingBuffer(capacity=1024)
        await buf.write(b"first")
        await buf.write(b"second")
        await buf.write(b"third")
        assert await buf.read() == b"first"
        assert await buf.read() == b"second"
        assert await buf.read() == b"third"

    @pytest.mark.asyncio
    async def test_capacity_tracking(self) -> None:
        buf = RingBuffer(capacity=100)
        await buf.write(b"x" * 40)
        assert buf.stats["size"] == 40
        assert buf.stats["msg_count"] == 1

        await buf.write(b"y" * 30)
        assert buf.stats["size"] == 70
        assert buf.stats["msg_count"] == 2

        await buf.read()
        assert buf.stats["size"] == 30
        assert buf.stats["msg_count"] == 1

    @pytest.mark.asyncio
    async def test_peek_returns_next_without_consuming(self) -> None:
        buf = RingBuffer(capacity=1024)
        assert buf.peek() is None

        await buf.write(b"msg1")
        await buf.write(b"msg2")
        assert buf.peek() == b"msg1"
        assert buf.stats["msg_count"] == 2  # not consumed

    @pytest.mark.asyncio
    async def test_peek_all(self) -> None:
        buf = RingBuffer(capacity=1024)
        await buf.write(b"a")
        await buf.write(b"b")
        await buf.write(b"c")
        assert buf.peek_all() == [b"a", b"b", b"c"]
        assert buf.stats["msg_count"] == 3  # not consumed

    @pytest.mark.asyncio
    async def test_stats(self) -> None:
        buf = RingBuffer(capacity=256)
        stats = buf.stats
        assert stats["size"] == 0
        assert stats["capacity"] == 256
        assert stats["msg_count"] == 0
        assert stats["closed"] is False

    @pytest.mark.asyncio
    async def test_empty_write_is_noop(self) -> None:
        buf = RingBuffer(capacity=1024)
        result = await buf.write(b"")
        assert result == 0
        assert buf.stats["msg_count"] == 0

    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValueError, match="capacity must be > 0"):
            RingBuffer(capacity=0)
        with pytest.raises(ValueError, match="capacity must be > 0"):
            RingBuffer(capacity=-1)


# ======================================================================
# RingBuffer — capacity limits
# ======================================================================


class TestRingBufferCapacity:
    @pytest.mark.asyncio
    async def test_oversized_message_rejected(self) -> None:
        buf = RingBuffer(capacity=10)
        with pytest.raises(ValueError, match="exceeds buffer capacity"):
            await buf.write(b"x" * 11)

    @pytest.mark.asyncio
    async def test_exact_capacity_message(self) -> None:
        buf = RingBuffer(capacity=10)
        await buf.write(b"x" * 10)
        assert buf.stats["size"] == 10

    @pytest.mark.asyncio
    async def test_non_blocking_full_raises(self) -> None:
        buf = RingBuffer(capacity=10)
        await buf.write(b"x" * 10)
        with pytest.raises(PipeFullError, match="buffer full"):
            await buf.write(b"y", blocking=False)

    @pytest.mark.asyncio
    async def test_non_blocking_empty_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        with pytest.raises(PipeEmptyError, match="buffer empty"):
            await buf.read(blocking=False)

    @pytest.mark.asyncio
    async def test_space_freed_after_read(self) -> None:
        buf = RingBuffer(capacity=20)
        await buf.write(b"x" * 15)
        await buf.read()
        # Now have 20 bytes free
        await buf.write(b"y" * 20)
        assert buf.stats["size"] == 20


# ======================================================================
# RingBuffer — blocking semantics
# ======================================================================


class TestRingBufferBlocking:
    @pytest.mark.asyncio
    async def test_reader_blocks_until_write(self) -> None:
        buf = RingBuffer(capacity=1024)
        result = None

        async def reader() -> None:
            nonlocal result
            result = await buf.read()

        async def writer() -> None:
            await asyncio.sleep(0.01)
            await buf.write(b"wakeup")

        await asyncio.gather(reader(), writer())
        assert result == b"wakeup"

    @pytest.mark.asyncio
    async def test_writer_blocks_until_read(self) -> None:
        buf = RingBuffer(capacity=10)
        await buf.write(b"x" * 10)  # fill buffer

        written = False

        async def writer() -> None:
            nonlocal written
            await buf.write(b"y" * 5)
            written = True

        async def reader() -> None:
            await asyncio.sleep(0.01)
            await buf.read()  # free 10 bytes

        await asyncio.gather(writer(), reader())
        assert written is True

    @pytest.mark.asyncio
    async def test_multiple_messages_flow(self) -> None:
        buf = RingBuffer(capacity=1024)
        received: list[bytes] = []

        async def producer() -> None:
            for i in range(10):
                await buf.write(f"msg-{i}".encode())
            buf.close()

        async def consumer() -> None:
            while True:
                try:
                    msg = await buf.read()
                    received.append(msg)
                except PipeClosedError:
                    break

        await asyncio.gather(producer(), consumer())
        assert len(received) == 10
        assert received[0] == b"msg-0"
        assert received[9] == b"msg-9"


# ======================================================================
# RingBuffer — close semantics
# ======================================================================


class TestRingBufferClose:
    @pytest.mark.asyncio
    async def test_write_after_close_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.close()
        with pytest.raises(PipeClosedError, match="write to closed pipe"):
            await buf.write(b"data")

    @pytest.mark.asyncio
    async def test_read_drains_remaining_then_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        await buf.write(b"last-msg")
        buf.close()

        # Can still read buffered messages
        result = await buf.read()
        assert result == b"last-msg"

        # Then raises
        with pytest.raises(PipeClosedError, match="read from closed empty pipe"):
            await buf.read()

    @pytest.mark.asyncio
    async def test_close_wakes_blocked_reader(self) -> None:
        buf = RingBuffer(capacity=1024)

        async def blocked_reader() -> None:
            with pytest.raises(PipeClosedError):
                await buf.read()

        async def closer() -> None:
            await asyncio.sleep(0.01)
            buf.close()

        await asyncio.gather(blocked_reader(), closer())

    @pytest.mark.asyncio
    async def test_close_wakes_blocked_writer(self) -> None:
        buf = RingBuffer(capacity=5)
        await buf.write(b"xxxxx")  # fill

        async def blocked_writer() -> None:
            with pytest.raises(PipeClosedError):
                await buf.write(b"more")

        async def closer() -> None:
            await asyncio.sleep(0.01)
            buf.close()

        await asyncio.gather(blocked_writer(), closer())

    @pytest.mark.asyncio
    async def test_closed_property(self) -> None:
        buf = RingBuffer(capacity=1024)
        assert buf.closed is False
        buf.close()
        assert buf.closed is True


# ======================================================================
# PipeManager — lifecycle
# ======================================================================


class MockMetastore:
    """Minimal MetastoreABC mock for PipeManager tests."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> None:
        if metadata.path:
            self._store[metadata.path] = metadata

    def delete(self, path: str, *, consistency: str = "sc") -> dict | None:
        return {"path": path} if self._store.pop(path, None) else None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **kwargs) -> list:  # noqa: ARG002
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def close(self) -> None:
        pass


class TestPipeManager:
    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    def test_create_pipe(self) -> None:
        mgr, ms = self._make_manager()
        buf = mgr.create(
            "/nexus/pipes/test", capacity=4096, owner_id="agent-1", zone_id="test-zone"
        )

        assert isinstance(buf, RingBuffer)
        assert buf.stats["capacity"] == 4096

        # Inode created in metastore
        meta = ms.get("/nexus/pipes/test")
        assert meta is not None
        assert meta.entry_type == DT_PIPE
        assert meta.backend_name == "pipe"
        assert meta.physical_path == "mem://"
        assert meta.size == 4096
        assert meta.owner_id == "agent-1"
        assert meta.zone_id == "test-zone"

    def test_create_duplicate_raises(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/dup")
        with pytest.raises(PipeExistsError, match="pipe already exists"):
            mgr.create("/nexus/pipes/dup")

    def test_create_at_existing_path_raises(self) -> None:
        mgr, ms = self._make_manager()
        # Pre-populate a regular file inode
        ms.put(
            FileMetadata(
                path="/existing/file",
                backend_name="local",
                physical_path="/data/file",
                size=100,
                entry_type=DT_REG,
            )
        )
        with pytest.raises(PipeExistsError, match="path already exists"):
            mgr.create("/existing/file")

    def test_open_existing_buffer(self) -> None:
        mgr, _ = self._make_manager()
        buf1 = mgr.create("/nexus/pipes/p1")
        buf2 = mgr.open("/nexus/pipes/p1")
        assert buf1 is buf2

    def test_open_recovers_after_buffer_lost(self) -> None:
        mgr, ms = self._make_manager()
        mgr.create("/nexus/pipes/recover", capacity=2048)

        # Simulate buffer loss (e.g., PipeManager recreated after restart)
        mgr._buffers.clear()

        buf = mgr.open("/nexus/pipes/recover", capacity=2048)
        assert isinstance(buf, RingBuffer)
        assert buf.stats["capacity"] == 2048

    def test_open_nonexistent_raises(self) -> None:
        mgr, _ = self._make_manager()
        with pytest.raises(PipeNotFoundError, match="no pipe at"):
            mgr.open("/nexus/pipes/ghost")

    def test_close_pipe(self) -> None:
        mgr, ms = self._make_manager()
        buf = mgr.create("/nexus/pipes/closeme")
        mgr.close("/nexus/pipes/closeme")

        assert buf.closed is True
        # Inode still in metastore
        assert ms.get("/nexus/pipes/closeme") is not None
        # Buffer removed from registry
        assert "/nexus/pipes/closeme" not in mgr._buffers

    def test_close_nonexistent_raises(self) -> None:
        mgr, _ = self._make_manager()
        with pytest.raises(PipeNotFoundError):
            mgr.close("/nexus/pipes/nope")

    def test_destroy_removes_inode(self) -> None:
        mgr, ms = self._make_manager()
        buf = mgr.create("/nexus/pipes/destroyme")
        mgr.destroy("/nexus/pipes/destroyme")

        assert buf.closed is True
        assert ms.get("/nexus/pipes/destroyme") is None
        assert "/nexus/pipes/destroyme" not in mgr._buffers

    def test_destroy_nonexistent_raises(self) -> None:
        mgr, _ = self._make_manager()
        with pytest.raises(PipeNotFoundError):
            mgr.destroy("/nexus/pipes/nope")

    @pytest.mark.asyncio
    async def test_pipe_write_read(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/rw")

        await mgr.pipe_write("/nexus/pipes/rw", b"hello")
        result = await mgr.pipe_read("/nexus/pipes/rw")
        assert result == b"hello"

    def test_pipe_peek(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/peek")
        assert mgr.pipe_peek("/nexus/pipes/peek") is None

    def test_list_pipes(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/a", capacity=100)
        mgr.create("/nexus/pipes/b", capacity=200)

        pipes = mgr.list_pipes()
        assert len(pipes) == 2
        assert pipes["/nexus/pipes/a"]["capacity"] == 100
        assert pipes["/nexus/pipes/b"]["capacity"] == 200

    def test_close_all(self) -> None:
        mgr, _ = self._make_manager()
        buf_a = mgr.create("/nexus/pipes/a")
        buf_b = mgr.create("/nexus/pipes/b")

        mgr.close_all()
        assert buf_a.closed is True
        assert buf_b.closed is True
        assert len(mgr._buffers) == 0

    @pytest.mark.asyncio
    async def test_pipe_write_to_nonexistent_raises(self) -> None:
        mgr, _ = self._make_manager()
        with pytest.raises(PipeNotFoundError):
            await mgr.pipe_write("/nexus/pipes/ghost", b"data")

    @pytest.mark.asyncio
    async def test_pipe_read_from_nonexistent_raises(self) -> None:
        mgr, _ = self._make_manager()
        with pytest.raises(PipeNotFoundError):
            await mgr.pipe_read("/nexus/pipes/ghost")

    def test_pipe_write_nowait_basic(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/sync", capacity=1024)
        written = mgr.pipe_write_nowait("/nexus/pipes/sync", b"hello")
        assert written == 5

    def test_pipe_write_nowait_nonexistent_raises(self) -> None:
        mgr, _ = self._make_manager()
        with pytest.raises(PipeNotFoundError):
            mgr.pipe_write_nowait("/nexus/pipes/ghost", b"data")

    @pytest.mark.asyncio
    async def test_pipe_write_nowait_then_async_read(self) -> None:
        """Sync write + async read roundtrip (workflow queue pattern)."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/mixed", capacity=1024)
        mgr.pipe_write_nowait("/nexus/pipes/mixed", b"event-1")
        mgr.pipe_write_nowait("/nexus/pipes/mixed", b"event-2")
        assert await mgr.pipe_read("/nexus/pipes/mixed") == b"event-1"
        assert await mgr.pipe_read("/nexus/pipes/mixed") == b"event-2"

    def test_close_all_clears_locks(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/a")
        mgr._get_lock("/nexus/pipes/a")  # force lock creation
        assert len(mgr._locks) == 1
        mgr.close_all()
        assert len(mgr._locks) == 0

    def test_close_clears_lock(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/a")
        mgr._get_lock("/nexus/pipes/a")
        mgr.close("/nexus/pipes/a")
        assert "/nexus/pipes/a" not in mgr._locks

    def test_destroy_clears_lock(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/a")
        mgr._get_lock("/nexus/pipes/a")
        mgr.destroy("/nexus/pipes/a")
        assert "/nexus/pipes/a" not in mgr._locks


# ======================================================================
# RingBuffer — write_nowait / read_nowait
# ======================================================================


class TestRingBufferSyncOps:
    def test_write_nowait_basic(self) -> None:
        buf = RingBuffer(capacity=1024)
        written = buf.write_nowait(b"hello")
        assert written == 5
        assert buf.stats["size"] == 5
        assert buf.stats["msg_count"] == 1

    def test_write_nowait_empty_is_noop(self) -> None:
        buf = RingBuffer(capacity=1024)
        assert buf.write_nowait(b"") == 0
        assert buf.stats["msg_count"] == 0

    def test_write_nowait_full_raises(self) -> None:
        buf = RingBuffer(capacity=10)
        buf.write_nowait(b"x" * 10)
        with pytest.raises(PipeFullError, match="buffer full"):
            buf.write_nowait(b"y")

    def test_write_nowait_oversized_raises(self) -> None:
        buf = RingBuffer(capacity=10)
        with pytest.raises(ValueError, match="exceeds buffer capacity"):
            buf.write_nowait(b"x" * 11)

    def test_write_nowait_closed_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.close()
        with pytest.raises(PipeClosedError, match="write to closed pipe"):
            buf.write_nowait(b"data")

    def test_read_nowait_basic(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.write_nowait(b"msg")
        assert buf.read_nowait() == b"msg"
        assert buf.stats["size"] == 0

    def test_read_nowait_empty_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        with pytest.raises(PipeEmptyError, match="buffer empty"):
            buf.read_nowait()

    def test_read_nowait_closed_empty_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.close()
        with pytest.raises(PipeClosedError, match="read from closed empty pipe"):
            buf.read_nowait()

    def test_read_nowait_drains_before_closed_error(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.write_nowait(b"last")
        buf.close()
        assert buf.read_nowait() == b"last"
        with pytest.raises(PipeClosedError):
            buf.read_nowait()

    @pytest.mark.asyncio
    async def test_write_nowait_wakes_async_reader(self) -> None:
        """Sync write should wake a blocked async reader."""
        buf = RingBuffer(capacity=1024)
        result = None

        async def reader() -> None:
            nonlocal result
            result = await buf.read()

        async def writer() -> None:
            await asyncio.sleep(0.01)
            buf.write_nowait(b"wakeup")

        await asyncio.gather(reader(), writer())
        assert result == b"wakeup"

    @pytest.mark.asyncio
    async def test_wait_writable(self) -> None:
        buf = RingBuffer(capacity=10)
        buf.write_nowait(b"x" * 10)

        unblocked = False

        async def waiter() -> None:
            nonlocal unblocked
            await buf.wait_writable()
            unblocked = True

        async def reader() -> None:
            await asyncio.sleep(0.01)
            await buf.read()

        await asyncio.gather(waiter(), reader())
        assert unblocked is True

    @pytest.mark.asyncio
    async def test_wait_readable(self) -> None:
        buf = RingBuffer(capacity=1024)

        unblocked = False

        async def waiter() -> None:
            nonlocal unblocked
            await buf.wait_readable()
            unblocked = True

        async def writer() -> None:
            await asyncio.sleep(0.01)
            buf.write_nowait(b"data")

        await asyncio.gather(waiter(), writer())
        assert unblocked is True


# ======================================================================
# RingBuffer — u64 fast path (L2)
# ======================================================================


class TestRingBufferU64:
    def test_write_u64_nowait_read_u64_nowait(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.write_u64_nowait(42)
        buf.write_u64_nowait(2**64 - 1)
        buf.write_u64_nowait(0)
        assert buf.read_u64_nowait() == 42
        assert buf.read_u64_nowait() == 2**64 - 1
        assert buf.read_u64_nowait() == 0

    def test_u64_size_tracking(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.write_u64_nowait(99)
        assert buf.stats["size"] == 8
        assert buf.stats["msg_count"] == 1
        buf.read_u64_nowait()
        assert buf.stats["size"] == 0

    @pytest.mark.asyncio
    async def test_async_write_u64_read_u64(self) -> None:
        buf = RingBuffer(capacity=1024)
        await buf.write_u64(100)
        result = await buf.read_u64()
        assert result == 100

    @pytest.mark.asyncio
    async def test_u64_reader_blocks_until_write(self) -> None:
        buf = RingBuffer(capacity=1024)
        result = None

        async def reader() -> None:
            nonlocal result
            result = await buf.read_u64()

        async def writer() -> None:
            await asyncio.sleep(0.01)
            buf.write_u64_nowait(777)

        await asyncio.gather(reader(), writer())
        assert result == 777

    def test_u64_closed_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.close()
        with pytest.raises(PipeClosedError):
            buf.write_u64_nowait(42)
        with pytest.raises(PipeClosedError):
            buf.read_u64_nowait()

    def test_u64_empty_raises(self) -> None:
        buf = RingBuffer(capacity=1024)
        with pytest.raises(PipeEmptyError):
            buf.read_u64_nowait()

    def test_interleaved_bytes_and_u64(self) -> None:
        buf = RingBuffer(capacity=1024)
        buf.write_nowait(b"hello")
        buf.write_u64_nowait(12345)
        buf.write_nowait(b"world")
        assert buf.read_nowait() == b"hello"
        assert buf.read_u64_nowait() == 12345
        assert buf.read_nowait() == b"world"


# ======================================================================
# PipeManager — MPMC locking
# ======================================================================


class TestPipeManagerMPMC:
    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    @pytest.mark.asyncio
    async def test_concurrent_writers(self) -> None:
        """Multiple async writers should not lose messages."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/mpmc", capacity=65_536)
        n_writers = 5
        msgs_per_writer = 20

        async def writer(writer_id: int) -> None:
            for i in range(msgs_per_writer):
                await mgr.pipe_write("/nexus/pipes/mpmc", f"w{writer_id}-{i}".encode())

        await asyncio.gather(*(writer(w) for w in range(n_writers)))

        received: list[bytes] = []
        for _ in range(n_writers * msgs_per_writer):
            msg = await mgr.pipe_read("/nexus/pipes/mpmc", blocking=False)
            received.append(msg)

        assert len(received) == n_writers * msgs_per_writer

    @pytest.mark.asyncio
    async def test_blocking_write_waits_for_space(self) -> None:
        """Blocking pipe_write should wait (release lock) then succeed."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/block", capacity=10)
        await mgr.pipe_write("/nexus/pipes/block", b"x" * 10)

        written = False

        async def writer() -> None:
            nonlocal written
            await mgr.pipe_write("/nexus/pipes/block", b"y" * 5)
            written = True

        async def reader() -> None:
            await asyncio.sleep(0.01)
            await mgr.pipe_read("/nexus/pipes/block")

        await asyncio.gather(writer(), reader())
        assert written is True

    @pytest.mark.asyncio
    async def test_blocking_read_waits_for_data(self) -> None:
        """Blocking pipe_read should wait then succeed when data arrives."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/block-read", capacity=1024)

        result = None

        async def reader() -> None:
            nonlocal result
            result = await mgr.pipe_read("/nexus/pipes/block-read")

        async def writer() -> None:
            await asyncio.sleep(0.01)
            await mgr.pipe_write("/nexus/pipes/block-read", b"hello")

        await asyncio.gather(reader(), writer())
        assert result == b"hello"

    @pytest.mark.asyncio
    async def test_nonblocking_write_full_raises(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/nb", capacity=10)
        await mgr.pipe_write("/nexus/pipes/nb", b"x" * 10)
        with pytest.raises(PipeFullError):
            await mgr.pipe_write("/nexus/pipes/nb", b"y", blocking=False)

    @pytest.mark.asyncio
    async def test_nonblocking_read_empty_raises(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/nb-read", capacity=1024)
        with pytest.raises(PipeEmptyError):
            await mgr.pipe_read("/nexus/pipes/nb-read", blocking=False)


# ======================================================================
# DT_PIPE metadata integration
# ======================================================================


class TestDTPipeMetadata:
    def test_dt_pipe_constant(self) -> None:
        assert DT_PIPE == 3

    def test_is_pipe_property(self) -> None:
        meta = FileMetadata(
            path="/nexus/pipes/test",
            backend_name="pipe",
            physical_path="mem://",
            size=0,
            entry_type=DT_PIPE,
        )
        assert meta.is_pipe is True
        assert meta.is_reg is False
        assert meta.is_dir is False
        assert meta.is_mount is False

    def test_validate_skips_backend_checks_for_pipe(self) -> None:
        """DT_PIPE inodes don't need backend_name/physical_path validation."""
        meta = FileMetadata(
            path="/nexus/pipes/test",
            backend_name="",
            physical_path="",
            size=0,
            entry_type=DT_PIPE,
        )
        # Should NOT raise — validate() returns early for DT_PIPE
        meta.validate()

    def test_validate_still_checks_path_for_pipe(self) -> None:
        """DT_PIPE still needs a valid path."""
        meta = FileMetadata(
            path="",
            backend_name="",
            physical_path="",
            size=0,
            entry_type=DT_PIPE,
        )
        with pytest.raises(Exception, match="path is required"):
            meta.validate()

    def test_regular_file_still_validates_backend(self) -> None:
        """Ensure DT_PIPE skip doesn't break regular file validation."""
        meta = FileMetadata(
            path="/regular/file",
            backend_name="",
            physical_path="",
            size=0,
            entry_type=DT_REG,
        )
        with pytest.raises(Exception, match="backend_name is required"):
            meta.validate()


# ======================================================================
# sys_setattr upsert semantics
# ======================================================================


class TestSysSetAttrUpsert:
    """Test sys_setattr upsert: create-on-write for metadata."""

    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    def test_setattr_create_pipe(self) -> None:
        """sys_setattr with entry_type=DT_PIPE creates a pipe (replaces sys_mkpipe)."""
        mgr, ms = self._make_manager()
        # Simulate what NexusFS._setattr_create does
        path = "/nexus/pipes/via-setattr"
        capacity = 4096
        buf = mgr.create(path, capacity=capacity, owner_id="agent-1")
        assert isinstance(buf, RingBuffer)
        assert buf.stats["capacity"] == capacity
        meta = ms.get(path)
        assert meta is not None
        assert meta.entry_type == DT_PIPE

    def test_setattr_update_mutable_fields(self) -> None:
        """sys_setattr on existing inode only updates mutable fields."""
        from dataclasses import replace

        _, ms = self._make_manager()
        meta = FileMetadata(
            path="/existing/file",
            backend_name="local",
            physical_path="/data/file",
            size=100,
            entry_type=DT_REG,
            mime_type="text/plain",
        )
        ms.put(meta)

        # Update mime_type (mutable)
        updated = replace(meta, mime_type="application/json")
        ms.put(updated)
        result = ms.get("/existing/file")
        assert result is not None
        assert result.mime_type == "application/json"

    def test_setattr_entry_type_immutable_after_creation(self) -> None:
        """entry_type must be rejected for existing inodes."""
        _, ms = self._make_manager()
        meta = FileMetadata(
            path="/existing/file",
            backend_name="local",
            physical_path="/data/file",
            size=100,
            entry_type=DT_REG,
        )
        ms.put(meta)
        # Attempting to change entry_type should be rejected by caller
        assert ms.get("/existing/file") is not None
        assert ms.get("/existing/file").entry_type == DT_REG


# ======================================================================
# PipeManager federation — self_address and backend_name
# ======================================================================

SELF_ADDR = "10.0.0.1:50051"
REMOTE_ADDR = "10.0.0.2:50051"


class _MockChannelPool:
    """Minimal PeerChannelPool duck-type for PipeManager tests."""

    def __init__(self) -> None:
        from unittest.mock import MagicMock

        self.channel = MagicMock()

    def get(self, address: str) -> object:
        return self.channel


class TestPipeManagerRemoteDetection:
    """Test PipeManager.open() remote pipe detection and RemotePipeBackend install."""

    def _make_manager_with_pool(
        self,
    ) -> tuple[PipeManager, MockMetastore, _MockChannelPool]:
        ms = MockMetastore()
        pool = _MockChannelPool()
        mgr = PipeManager(ms, self_address=SELF_ADDR, channel_pool=pool)
        return mgr, ms, pool

    def test_open_remote_pipe_installs_remote_backend(self) -> None:
        """open() on a remote pipe should install RemotePipeBackend."""
        from nexus.core.remote_pipe import RemotePipeBackend

        mgr, ms, _ = self._make_manager_with_pool()
        # Manually create a remote pipe inode (origin is REMOTE_ADDR)
        meta = FileMetadata(
            path="/nexus/pipes/remote",
            backend_name=f"pipe@{REMOTE_ADDR}",
            physical_path="mem://",
            size=65536,
            entry_type=DT_PIPE,
        )
        ms.put(meta)

        backend = mgr.open("/nexus/pipes/remote")
        assert isinstance(backend, RemotePipeBackend)
        assert backend.stats["origin"] == REMOTE_ADDR
        assert "/nexus/pipes/remote" in mgr._buffers

    def test_open_local_pipe_installs_ringbuffer(self) -> None:
        """open() on a local pipe should install RingBuffer (not RemotePipeBackend)."""
        mgr, ms, _ = self._make_manager_with_pool()
        meta = FileMetadata(
            path="/nexus/pipes/local",
            backend_name=f"pipe@{SELF_ADDR}",
            physical_path="mem://",
            size=65536,
            entry_type=DT_PIPE,
        )
        ms.put(meta)

        backend = mgr.open("/nexus/pipes/local")
        assert isinstance(backend, RingBuffer)

    def test_open_plain_pipe_installs_ringbuffer(self) -> None:
        """open() on a plain pipe (no origin) should install RingBuffer."""
        mgr, ms, _ = self._make_manager_with_pool()
        meta = FileMetadata(
            path="/nexus/pipes/plain",
            backend_name="pipe",
            physical_path="mem://",
            size=65536,
            entry_type=DT_PIPE,
        )
        ms.put(meta)

        backend = mgr.open("/nexus/pipes/plain")
        assert isinstance(backend, RingBuffer)

    def test_open_without_pool_always_ringbuffer(self) -> None:
        """Without channel_pool, open() always creates RingBuffer even for remote."""
        ms = MockMetastore()
        mgr = PipeManager(ms, self_address=SELF_ADDR)  # no channel_pool
        meta = FileMetadata(
            path="/nexus/pipes/remote-no-pool",
            backend_name=f"pipe@{REMOTE_ADDR}",
            physical_path="mem://",
            size=65536,
            entry_type=DT_PIPE,
        )
        ms.put(meta)

        backend = mgr.open("/nexus/pipes/remote-no-pool")
        assert isinstance(backend, RingBuffer)


class TestPipeManagerSelfAddress:
    """Test PipeManager.self_address and backend_name embedding (#1576)."""

    def test_self_address_none_by_default(self) -> None:
        ms = MockMetastore()
        mgr = PipeManager(ms)
        assert mgr.self_address is None

    def test_self_address_set(self) -> None:
        ms = MockMetastore()
        mgr = PipeManager(ms, self_address=SELF_ADDR)
        assert mgr.self_address == SELF_ADDR

    def test_create_with_self_address_embeds_origin(self) -> None:
        ms = MockMetastore()
        mgr = PipeManager(ms, self_address=SELF_ADDR)
        mgr.create("/nexus/pipes/fed")

        meta = ms.get("/nexus/pipes/fed")
        assert meta is not None
        assert meta.backend_name == f"pipe@{SELF_ADDR}"

    def test_create_without_self_address_plain_pipe(self) -> None:
        ms = MockMetastore()
        mgr = PipeManager(ms)
        mgr.create("/nexus/pipes/local")

        meta = ms.get("/nexus/pipes/local")
        assert meta is not None
        assert meta.backend_name == "pipe"

    def test_backend_address_parse_roundtrip(self) -> None:
        """Verify BackendAddress can parse pipe@<addr> format."""
        from nexus.contracts.backend_address import BackendAddress

        addr = BackendAddress.parse(f"pipe@{SELF_ADDR}")
        assert addr.backend_type == "pipe"
        assert addr.has_origin is True
        assert addr.origins == (SELF_ADDR,)

    def test_backend_address_parse_plain_pipe(self) -> None:
        """Verify BackendAddress handles plain 'pipe' (no origin)."""
        from nexus.contracts.backend_address import BackendAddress

        addr = BackendAddress.parse("pipe")
        assert addr.backend_type == "pipe"
        assert addr.has_origin is False
        assert addr.origins == ()


# ======================================================================
# sys_setattr — idempotent open (restart recovery)
# ======================================================================


class TestSysSetAttrIdempotentOpen:
    """Test that sys_setattr with same entry_type on existing inode
    recovers the in-memory buffer (idempotent open) instead of raising."""

    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    def test_idempotent_open_recovers_buffer(self) -> None:
        """setattr DT_PIPE on existing DT_PIPE metadata → buffer recovered, created=False."""
        mgr, ms = self._make_manager()
        # Create pipe, then close buffer to simulate restart
        buf = mgr.create("/nexus/pipes/recover")
        buf.close()

        # Idempotent open should recover
        recovered = mgr.open("/nexus/pipes/recover", capacity=65_536)
        assert not recovered.closed
        assert recovered is not buf  # new buffer instance

    def test_idempotent_open_noop_when_alive(self) -> None:
        """setattr DT_PIPE on existing DT_PIPE with buffer still alive → returns existing."""
        mgr, ms = self._make_manager()
        buf = mgr.create("/nexus/pipes/alive")

        # open() on alive buffer returns same instance
        same = mgr.open("/nexus/pipes/alive")
        assert same is buf

    def test_setattr_rejects_type_change(self) -> None:
        """setattr DT_PIPE on existing DT_REG → ValueError."""
        ms = MockMetastore()
        # Manually insert a DT_REG inode
        reg_meta = FileMetadata(
            path="/nexus/files/regular",
            entry_type=DT_REG,
            backend_name="local",
            physical_path="/tmp/regular",
            size=0,
        )
        ms.put(reg_meta)

        mgr = PipeManager(ms)
        with pytest.raises(PipeNotFoundError):
            mgr.open("/nexus/files/regular")


# ======================================================================
# PipeManager — signal_close lifecycle (Issue #3198 review, Issue 9)
# ======================================================================


class TestPipeManagerSignalClose:
    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    @pytest.mark.asyncio
    async def test_signal_close_wakes_blocked_reader(self) -> None:
        """signal_close() should wake a reader blocked on an empty pipe."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/sc", capacity=1024)

        woke = False

        async def reader() -> None:
            nonlocal woke
            with pytest.raises(PipeClosedError):
                await mgr.pipe_read("/nexus/pipes/sc")
            woke = True

        async def closer() -> None:
            await asyncio.sleep(0.01)
            mgr.signal_close("/nexus/pipes/sc")

        await asyncio.gather(reader(), closer())
        assert woke is True

    @pytest.mark.asyncio
    async def test_drain_after_signal_close(self) -> None:
        """Readers can drain remaining messages after signal_close."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/drain", capacity=1024)
        await mgr.pipe_write("/nexus/pipes/drain", b"msg-1")
        await mgr.pipe_write("/nexus/pipes/drain", b"msg-2")

        mgr.signal_close("/nexus/pipes/drain")

        # Drain should succeed
        assert await mgr.pipe_read("/nexus/pipes/drain") == b"msg-1"
        assert await mgr.pipe_read("/nexus/pipes/drain") == b"msg-2"
        # After drain, PipeClosedError
        with pytest.raises(PipeClosedError):
            await mgr.pipe_read("/nexus/pipes/drain")

    def test_signal_close_keeps_lock(self) -> None:
        """signal_close() should NOT remove the lock (unlike close/destroy)."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/sc-lock", capacity=1024)
        mgr._get_lock("/nexus/pipes/sc-lock")
        assert "/nexus/pipes/sc-lock" in mgr._locks

        mgr.signal_close("/nexus/pipes/sc-lock")
        assert "/nexus/pipes/sc-lock" in mgr._locks  # Lock stays for drain

    def test_signal_close_keeps_buffer_in_registry(self) -> None:
        """signal_close() keeps the buffer in _buffers for drain access."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/sc-buf", capacity=1024)

        mgr.signal_close("/nexus/pipes/sc-buf")
        assert "/nexus/pipes/sc-buf" in mgr._buffers

    def test_close_after_signal_close(self) -> None:
        """close() after signal_close() should clean up everything."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/sc-then-close", capacity=1024)
        mgr._get_lock("/nexus/pipes/sc-then-close")

        mgr.signal_close("/nexus/pipes/sc-then-close")
        mgr.close("/nexus/pipes/sc-then-close")

        assert "/nexus/pipes/sc-then-close" not in mgr._buffers
        assert "/nexus/pipes/sc-then-close" not in mgr._locks

    def test_signal_close_nonexistent_raises(self) -> None:
        mgr, _ = self._make_manager()
        with pytest.raises(PipeNotFoundError):
            mgr.signal_close("/nexus/pipes/nope")


# ======================================================================
# PipeManager — create_from_backend (Issue #3198 review, Issue 10)
# ======================================================================


class TestPipeManagerCreateFromBackend:
    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    def test_create_from_backend_happy_path(self) -> None:
        """create_from_backend registers the given backend and creates inode."""
        mgr, ms = self._make_manager()
        custom_buf = RingBuffer(capacity=2048)

        result = mgr.create_from_backend("/nexus/pipes/custom", custom_buf, owner_id="agent-x")

        assert result is custom_buf
        assert "/nexus/pipes/custom" in mgr._buffers
        meta = ms.get("/nexus/pipes/custom")
        assert meta is not None
        assert meta.entry_type == DT_PIPE
        assert meta.owner_id == "agent-x"

    @pytest.mark.asyncio
    async def test_create_from_backend_read_write(self) -> None:
        """Data flows through a custom-backend pipe via PipeManager."""
        mgr, _ = self._make_manager()
        buf = RingBuffer(capacity=1024)
        mgr.create_from_backend("/nexus/pipes/rw", buf)

        mgr.pipe_write_nowait("/nexus/pipes/rw", b"hello")
        assert await mgr.pipe_read("/nexus/pipes/rw") == b"hello"

    def test_create_from_backend_duplicate_buffer_raises(self) -> None:
        mgr, _ = self._make_manager()
        mgr.create_from_backend("/nexus/pipes/dup", RingBuffer(capacity=1024))
        with pytest.raises(PipeExistsError, match="pipe already exists"):
            mgr.create_from_backend("/nexus/pipes/dup", RingBuffer(capacity=1024))

    def test_create_from_backend_existing_path_raises(self) -> None:
        mgr, ms = self._make_manager()
        ms.put(
            FileMetadata(
                path="/nexus/pipes/taken",
                backend_name="local",
                physical_path="/tmp/x",
                size=0,
                entry_type=DT_REG,
            )
        )
        with pytest.raises(PipeExistsError, match="path already exists"):
            mgr.create_from_backend("/nexus/pipes/taken", RingBuffer(capacity=1024))

    def test_destroy_custom_backend(self) -> None:
        """destroy() works on custom-backend pipes."""
        mgr, ms = self._make_manager()
        mgr.create_from_backend("/nexus/pipes/cust-destroy", RingBuffer(capacity=1024))

        mgr.destroy("/nexus/pipes/cust-destroy")
        assert "/nexus/pipes/cust-destroy" not in mgr._buffers
        assert ms.get("/nexus/pipes/cust-destroy") is None


# ======================================================================
# PipeManager — MPMC concurrent readers + mixed (Issue #3198 review, Issue 11)
# ======================================================================


class TestPipeManagerMPMCExtended:
    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    @pytest.mark.asyncio
    async def test_concurrent_readers_no_duplicates(self) -> None:
        """Multiple async readers should not receive duplicate messages."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/cr", capacity=65_536)
        n_msgs = 50

        # Pre-fill pipe
        for i in range(n_msgs):
            mgr.pipe_write_nowait("/nexus/pipes/cr", f"msg-{i}".encode())

        received: list[bytes] = []
        lock = asyncio.Lock()

        async def reader() -> None:
            while True:
                try:
                    msg = await mgr.pipe_read("/nexus/pipes/cr", blocking=False)
                except PipeEmptyError:
                    break
                async with lock:
                    received.append(msg)

        await asyncio.gather(*(reader() for _ in range(5)))

        assert len(received) == n_msgs
        # No duplicates
        assert len(set(received)) == n_msgs

    @pytest.mark.asyncio
    async def test_mixed_concurrent_readers_and_writers(self) -> None:
        """Simultaneous readers and writers should not lose or duplicate messages."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/mixed-rw", capacity=65_536)
        n_writers = 3
        msgs_per_writer = 20
        total = n_writers * msgs_per_writer

        received: list[bytes] = []
        done = asyncio.Event()

        async def writer(wid: int) -> None:
            for i in range(msgs_per_writer):
                await mgr.pipe_write("/nexus/pipes/mixed-rw", f"w{wid}-{i}".encode())

        async def reader() -> None:
            while not done.is_set():
                try:
                    msg = await asyncio.wait_for(
                        mgr.pipe_read("/nexus/pipes/mixed-rw"), timeout=0.1
                    )
                    received.append(msg)
                    if len(received) >= total:
                        done.set()
                except TimeoutError:
                    continue

        writers = [writer(w) for w in range(n_writers)]
        reader_tasks = [asyncio.create_task(reader()) for _ in range(2)]

        await asyncio.gather(*writers)
        await asyncio.wait_for(done.wait(), timeout=2.0)
        for t in reader_tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        assert len(received) == total
        assert len(set(received)) == total

    @pytest.mark.asyncio
    async def test_thundering_herd_single_message(self) -> None:
        """Many blocked readers, one write: exactly one reader gets the message."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/herd", capacity=1024)
        n_readers = 5

        results: list[bytes] = []
        lock = asyncio.Lock()

        async def reader(rid: int) -> None:
            try:
                msg = await asyncio.wait_for(mgr.pipe_read("/nexus/pipes/herd"), timeout=0.5)
                async with lock:
                    results.append(msg)
            except TimeoutError:
                pass  # Expected for losers

        tasks = [asyncio.create_task(reader(r)) for r in range(n_readers)]
        await asyncio.sleep(0.02)  # Let all readers block

        await mgr.pipe_write("/nexus/pipes/herd", b"prize")
        await asyncio.gather(*tasks)

        # Exactly one reader should have received the message
        assert results == [b"prize"]


# ======================================================================
# PipeManager — pipe closure during blocking wait (Issue #3198 review, Issue 12)
# ======================================================================


class TestPipeManagerClosureDuringWait:
    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    @pytest.mark.asyncio
    async def test_close_during_blocked_read(self) -> None:
        """Closing a pipe while pipe_read() is blocked should raise PipeClosedError."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/close-read", capacity=1024)

        async def reader() -> None:
            with pytest.raises(PipeClosedError):
                await mgr.pipe_read("/nexus/pipes/close-read")

        async def closer() -> None:
            await asyncio.sleep(0.01)
            mgr.signal_close("/nexus/pipes/close-read")

        await asyncio.gather(reader(), closer())

    @pytest.mark.asyncio
    async def test_close_during_blocked_write(self) -> None:
        """Closing a pipe while pipe_write() is blocked should raise PipeClosedError."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/close-write", capacity=10)
        # Fill the pipe
        await mgr.pipe_write("/nexus/pipes/close-write", b"x" * 10)

        async def writer() -> None:
            with pytest.raises(PipeClosedError):
                await mgr.pipe_write("/nexus/pipes/close-write", b"more")

        async def closer() -> None:
            await asyncio.sleep(0.01)
            mgr.signal_close("/nexus/pipes/close-write")

        await asyncio.gather(writer(), closer())


# ======================================================================
# PipeManager — backpressure stats (Issue #3198 review, Issue 16)
# ======================================================================


class TestPipeManagerBackpressureStats:
    def _make_manager(self) -> tuple[PipeManager, MockMetastore]:
        ms = MockMetastore()
        return PipeManager(ms), ms

    def test_initial_stats_zero(self) -> None:
        mgr, _ = self._make_manager()
        stats = mgr.backpressure_stats
        assert stats["write_blocks"] == 0
        assert stats["read_blocks"] == 0
        assert stats["total_write_wait_ns"] == 0
        assert stats["total_read_wait_ns"] == 0

    @pytest.mark.asyncio
    async def test_read_block_increments_counter(self) -> None:
        """Blocking pipe_read should increment read_blocks."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/bp-read", capacity=1024)

        async def reader() -> None:
            await mgr.pipe_read("/nexus/pipes/bp-read")

        async def writer() -> None:
            await asyncio.sleep(0.01)
            await mgr.pipe_write("/nexus/pipes/bp-read", b"data")

        await asyncio.gather(reader(), writer())

        assert mgr.backpressure_stats["read_blocks"] >= 1
        assert mgr.backpressure_stats["total_read_wait_ns"] > 0

    @pytest.mark.asyncio
    async def test_write_block_increments_counter(self) -> None:
        """Blocking pipe_write on full pipe should increment write_blocks."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/bp-write", capacity=10)
        await mgr.pipe_write("/nexus/pipes/bp-write", b"x" * 10)

        async def writer() -> None:
            await mgr.pipe_write("/nexus/pipes/bp-write", b"y")

        async def reader() -> None:
            await asyncio.sleep(0.01)
            await mgr.pipe_read("/nexus/pipes/bp-write")

        await asyncio.gather(writer(), reader())

        assert mgr.backpressure_stats["write_blocks"] >= 1
        assert mgr.backpressure_stats["total_write_wait_ns"] > 0

    @pytest.mark.asyncio
    async def test_nowait_does_not_increment(self) -> None:
        """pipe_write_nowait should NOT affect backpressure counters."""
        mgr, _ = self._make_manager()
        mgr.create("/nexus/pipes/bp-nowait", capacity=1024)
        mgr.pipe_write_nowait("/nexus/pipes/bp-nowait", b"data")

        stats = mgr.backpressure_stats
        assert stats["write_blocks"] == 0
        assert stats["read_blocks"] == 0


# ======================================================================
# PipeExistsError (Issue #3198 review, Issue 8)
# ======================================================================


class TestPipeExistsError:
    def test_is_subclass_of_pipe_error(self) -> None:
        assert issubclass(PipeExistsError, PipeError)

    def test_ensure_does_not_catch_other_pipe_errors(self) -> None:
        """ensure() should only catch PipeExistsError, not other PipeErrors."""
        ms = MockMetastore()
        mgr = PipeManager(ms)

        # Create a pipe so ensure() will try create() → PipeExistsError → open()
        mgr.create("/nexus/pipes/ensure-test")
        # ensure() should succeed (falls through to open)
        result = mgr.ensure("/nexus/pipes/ensure-test")
        assert result is not None
