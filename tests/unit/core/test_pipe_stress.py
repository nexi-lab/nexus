"""Stress tests for DT_PIPE kernel IPC under backpressure, MPMC, and closure.

Issue #2761: Multi-agent orchestration requires pipes to handle:
- Backpressure (buffer full → writer blocks → reader frees space → writer resumes)
- Pipe closure during pending operations (graceful error propagation)
- MPMC contention (10+ writers via PipeManager, message ordering and completeness)
- Timeout support (prevent deadlocks when workers die)

See: src/nexus/core/pipe.py, src/nexus/system_services/pipe_manager.py
"""

import asyncio

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.core.pipe import (
    PipeClosedError,
    PipeEmptyError,
    PipeFullError,
    PipeTimeoutError,
    RingBuffer,
)
from nexus.system_services.pipe_manager import PipeManager

# ======================================================================
# Helper: MockMetastore (reused from test_pipe.py)
# ======================================================================


class MockMetastore:
    """Minimal MetastoreABC mock for PipeManager tests."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> None:  # noqa: ARG002
        if metadata.path:
            self._store[metadata.path] = metadata

    def delete(self, path: str, *, consistency: str = "sc") -> dict | None:  # noqa: ARG002
        return {"path": path} if self._store.pop(path, None) else None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **kwargs) -> list:  # noqa: ARG002
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def close(self) -> None:
        pass


def _make_manager() -> tuple[PipeManager, MockMetastore]:
    ms = MockMetastore()
    return PipeManager(ms, zone_id="stress-zone"), ms


# ======================================================================
# Backpressure tests
# ======================================================================


class TestBackpressure:
    """Verify that full pipes block writers and resume after reads."""

    @pytest.mark.asyncio
    async def test_backpressure_blocks_then_resumes(self) -> None:
        """Writer blocks when pipe full, unblocks after reader frees space."""
        buf = RingBuffer(capacity=20)
        # Fill the buffer
        await buf.write(b"x" * 20)
        assert buf.stats["size"] == 20

        write_completed = False

        async def writer() -> None:
            nonlocal write_completed
            # This should block until space is freed
            await buf.write(b"new-data")
            write_completed = True

        async def reader() -> None:
            await asyncio.sleep(0.02)
            # Free 20 bytes
            await buf.read()

        await asyncio.gather(writer(), reader())
        assert write_completed is True
        assert buf.stats["size"] == 8  # "new-data" is 8 bytes

    @pytest.mark.asyncio
    async def test_backpressure_multiple_writers_queued(self) -> None:
        """Multiple writers block on a small pipe, all eventually succeed."""
        buf = RingBuffer(capacity=30)
        # Fill with 25 bytes, only 5 left
        await buf.write(b"x" * 25)

        results: list[int] = []

        async def writer(idx: int) -> None:
            await buf.write(f"w{idx}".encode())  # 2 bytes each
            results.append(idx)

        async def reader() -> None:
            for _ in range(5):
                await asyncio.sleep(0.01)
                await buf.read()

        # 10 writers each needing 2 bytes, reader frees space gradually
        writers = [writer(i) for i in range(10)]
        await asyncio.gather(*writers, reader())

        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_backpressure_nonblocking_raises_immediately(self) -> None:
        """Non-blocking write on full pipe raises PipeFullError without waiting."""
        buf = RingBuffer(capacity=10)
        await buf.write(b"x" * 10)

        with pytest.raises(PipeFullError):
            await buf.write(b"y", blocking=False)


# ======================================================================
# Closure during pending operations
# ======================================================================


class TestClosureDuringOperations:
    """Verify graceful error propagation when pipe closes mid-operation."""

    @pytest.mark.asyncio
    async def test_closure_during_pending_write(self) -> None:
        """Writer blocked on full pipe gets PipeClosedError when pipe closes."""
        buf = RingBuffer(capacity=10)
        await buf.write(b"x" * 10)

        async def blocked_writer() -> None:
            with pytest.raises(PipeClosedError, match="write to closed pipe"):
                await buf.write(b"more-data")

        async def closer() -> None:
            await asyncio.sleep(0.02)
            buf.close()

        await asyncio.gather(blocked_writer(), closer())

    @pytest.mark.asyncio
    async def test_closure_during_pending_read(self) -> None:
        """Reader blocked on empty pipe gets PipeClosedError when pipe closes."""
        buf = RingBuffer(capacity=1024)

        async def blocked_reader() -> None:
            with pytest.raises(PipeClosedError, match="read from closed empty pipe"):
                await buf.read()

        async def closer() -> None:
            await asyncio.sleep(0.02)
            buf.close()

        await asyncio.gather(blocked_reader(), closer())

    @pytest.mark.asyncio
    async def test_closure_drains_remaining_then_raises(self) -> None:
        """After close, buffered messages can still be read, then raises."""
        buf = RingBuffer(capacity=1024)
        await buf.write(b"msg-1")
        await buf.write(b"msg-2")
        buf.close()

        assert await buf.read() == b"msg-1"
        assert await buf.read() == b"msg-2"
        with pytest.raises(PipeClosedError):
            await buf.read()

    @pytest.mark.asyncio
    async def test_pipe_manager_close_wakes_all_waiters(self) -> None:
        """PipeManager.close() should wake all blocked readers/writers."""
        mgr, _ = _make_manager()
        mgr.create("/pipes/close-test", capacity=10)
        await mgr.pipe_write("/pipes/close-test", b"x" * 10)

        errors: list[str] = []

        async def blocked_writer() -> None:
            try:
                await mgr.pipe_write("/pipes/close-test", b"more")
            except (PipeClosedError, PipeFullError):
                errors.append("writer")

        async def closer() -> None:
            await asyncio.sleep(0.02)
            mgr.close("/pipes/close-test")

        await asyncio.gather(blocked_writer(), closer())
        assert "writer" in errors


# ======================================================================
# MPMC contention via PipeManager
# ======================================================================


class TestMPMCContention:
    """Verify PipeManager handles multiple producers/consumers correctly."""

    @pytest.mark.asyncio
    async def test_mpmc_10_writers_no_lost_messages(self) -> None:
        """10 concurrent writers through PipeManager, no messages lost."""
        mgr, _ = _make_manager()
        mgr.create("/pipes/mpmc", capacity=65_536)

        n_writers = 10
        msgs_per_writer = 50

        async def writer(wid: int) -> None:
            for i in range(msgs_per_writer):
                await mgr.pipe_write("/pipes/mpmc", f"w{wid}-{i}".encode())

        await asyncio.gather(*(writer(w) for w in range(n_writers)))

        received: list[bytes] = []
        for _ in range(n_writers * msgs_per_writer):
            msg = await mgr.pipe_read("/pipes/mpmc", blocking=False)
            received.append(msg)

        assert len(received) == n_writers * msgs_per_writer

        # Verify no duplicates
        assert len(set(received)) == n_writers * msgs_per_writer

    @pytest.mark.asyncio
    async def test_mpmc_writers_and_readers_concurrent(self) -> None:
        """Concurrent writers and readers, all messages eventually delivered."""
        mgr, _ = _make_manager()
        mgr.create("/pipes/mpmc-rw", capacity=1024)

        n_writers = 5
        msgs_per_writer = 20
        total = n_writers * msgs_per_writer
        received: list[bytes] = []
        done = asyncio.Event()

        async def writer(wid: int) -> None:
            for i in range(msgs_per_writer):
                await mgr.pipe_write("/pipes/mpmc-rw", f"w{wid}-{i}".encode())

        async def reader() -> None:
            while len(received) < total:
                try:
                    msg = await mgr.pipe_read("/pipes/mpmc-rw", blocking=False)
                    received.append(msg)
                except PipeEmptyError:
                    await asyncio.sleep(0.001)
            done.set()

        writers = [writer(w) for w in range(n_writers)]
        await asyncio.gather(*writers, reader())

        assert len(received) == total
        assert len(set(received)) == total  # no duplicates

    @pytest.mark.asyncio
    async def test_mpmc_small_buffer_backpressure(self) -> None:
        """Writers block when small buffer is full under MPMC contention."""
        mgr, _ = _make_manager()
        mgr.create("/pipes/mpmc-small", capacity=50)

        n_writers = 5
        msgs_per_writer = 10
        total = n_writers * msgs_per_writer
        received: list[bytes] = []

        async def writer(wid: int) -> None:
            for i in range(msgs_per_writer):
                await mgr.pipe_write("/pipes/mpmc-small", f"w{wid}-{i}".encode())

        async def reader() -> None:
            while len(received) < total:
                try:
                    msg = await mgr.pipe_read("/pipes/mpmc-small", blocking=False)
                    received.append(msg)
                except PipeEmptyError:
                    await asyncio.sleep(0.001)

        writers = [writer(w) for w in range(n_writers)]
        await asyncio.gather(*writers, reader())

        assert len(received) == total


# ======================================================================
# Timeout support (Issue #8 fix)
# ======================================================================


class TestPipeTimeout:
    """Verify timeout prevents deadlocks in copilot/worker pipes."""

    @pytest.mark.asyncio
    async def test_read_timeout_on_empty_pipe(self) -> None:
        """Read with timeout raises PipeTimeoutError on empty pipe."""
        buf = RingBuffer(capacity=1024)
        with pytest.raises(PipeTimeoutError, match="read timed out"):
            await buf.read(timeout=0.05)

    @pytest.mark.asyncio
    async def test_write_timeout_on_full_pipe(self) -> None:
        """Write with timeout raises PipeTimeoutError on full pipe."""
        buf = RingBuffer(capacity=10)
        await buf.write(b"x" * 10)
        with pytest.raises(PipeTimeoutError, match="write timed out"):
            await buf.write(b"y", timeout=0.05)

    @pytest.mark.asyncio
    async def test_read_timeout_succeeds_if_data_arrives(self) -> None:
        """Read with timeout succeeds if data arrives before deadline."""
        buf = RingBuffer(capacity=1024)

        async def delayed_write() -> None:
            await asyncio.sleep(0.02)
            await buf.write(b"arrived")

        async def timed_read() -> bytes:
            return await buf.read(timeout=1.0)

        _, result = await asyncio.gather(delayed_write(), timed_read())
        assert result == b"arrived"

    @pytest.mark.asyncio
    async def test_write_timeout_succeeds_if_space_freed(self) -> None:
        """Write with timeout succeeds if space freed before deadline."""
        buf = RingBuffer(capacity=10)
        await buf.write(b"x" * 10)

        async def delayed_read() -> None:
            await asyncio.sleep(0.02)
            await buf.read()

        async def timed_write() -> int:
            return await buf.write(b"y" * 5, timeout=1.0)

        _, result = await asyncio.gather(delayed_read(), timed_write())
        assert result == 5

    @pytest.mark.asyncio
    async def test_timeout_none_blocks_indefinitely(self) -> None:
        """timeout=None (default) blocks until data/space available."""
        buf = RingBuffer(capacity=1024)
        result = None

        async def reader() -> None:
            nonlocal result
            result = await buf.read(timeout=None)  # blocks

        async def writer() -> None:
            await asyncio.sleep(0.02)
            await buf.write(b"wakeup")

        await asyncio.gather(reader(), writer())
        assert result == b"wakeup"


# ======================================================================
# Message ordering guarantees
# ======================================================================


class TestMessageOrdering:
    """Verify FIFO ordering under various conditions."""

    @pytest.mark.asyncio
    async def test_fifo_under_backpressure(self) -> None:
        """Messages maintain FIFO order even through backpressure cycles."""
        buf = RingBuffer(capacity=30)
        received: list[bytes] = []

        async def producer() -> None:
            for i in range(20):
                await buf.write(f"msg-{i:03d}".encode())  # 7 bytes each
            buf.close()

        async def consumer() -> None:
            while True:
                try:
                    msg = await buf.read()
                    received.append(msg)
                except PipeClosedError:
                    break

        await asyncio.gather(producer(), consumer())
        assert len(received) == 20
        for i, msg in enumerate(received):
            assert msg == f"msg-{i:03d}".encode()

    @pytest.mark.asyncio
    async def test_per_writer_ordering_preserved(self) -> None:
        """Within each writer, message order is preserved under MPMC."""
        mgr, _ = _make_manager()
        mgr.create("/pipes/order", capacity=65_536)

        n_writers = 5
        msgs_per_writer = 30

        async def writer(wid: int) -> None:
            for i in range(msgs_per_writer):
                await mgr.pipe_write("/pipes/order", f"w{wid}-{i:03d}".encode())

        await asyncio.gather(*(writer(w) for w in range(n_writers)))

        # Collect all messages
        received: list[bytes] = []
        for _ in range(n_writers * msgs_per_writer):
            msg = await mgr.pipe_read("/pipes/order", blocking=False)
            received.append(msg)

        # Group by writer and verify per-writer ordering
        by_writer: dict[str, list[int]] = {}
        for msg in received:
            text = msg.decode()
            wid, idx = text.split("-", 1)
            by_writer.setdefault(wid, []).append(int(idx))

        for wid, indices in by_writer.items():
            assert indices == sorted(indices), f"Writer {wid} messages out of order: {indices}"
