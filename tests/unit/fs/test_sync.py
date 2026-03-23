"""Tests for SyncNexusFS -- the anyio-based sync wrapper.

Covers four scenarios:
1. No event loop   (normal Python script)
2. Existing loop   (Jupyter-style, run sync wrapper from inside async code)
3. Concurrent threads (10 threads calling read() in parallel)
4. Error propagation (async exception surfaces without portal wrapping)
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock

import anyio.to_thread
import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.fs._sync import SyncNexusFS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_async_fs(**overrides: object) -> AsyncMock:
    """Build an AsyncMock that mimics the async NexusFS facade."""
    mock = AsyncMock()
    for attr, value in overrides.items():
        getattr(mock, attr).return_value = value
    return mock


# ---------------------------------------------------------------------------
# 1. No event loop -- normal script context
# ---------------------------------------------------------------------------


class TestNoEventLoop:
    """Call SyncNexusFS from a plain synchronous context (no running loop)."""

    def test_read_returns_bytes(self) -> None:
        mock_fs = _make_mock_async_fs(read=b"hello world")
        sync_fs = SyncNexusFS(mock_fs)

        result = sync_fs.read("/s3/bucket/file.txt")

        assert result == b"hello world"
        mock_fs.read.assert_awaited_once_with("/s3/bucket/file.txt")

    def test_write_returns_metadata(self) -> None:
        mock_fs = _make_mock_async_fs(write={"etag": "abc123"})
        sync_fs = SyncNexusFS(mock_fs)

        result = sync_fs.write("/s3/bucket/out.bin", b"data")

        assert result == {"etag": "abc123"}
        mock_fs.write.assert_awaited_once_with("/s3/bucket/out.bin", b"data")

    def test_ls_passes_detail_kwarg(self) -> None:
        entries = [{"name": "a.txt", "size": 10}]
        mock_fs = _make_mock_async_fs(ls=entries)
        sync_fs = SyncNexusFS(mock_fs)

        result = sync_fs.ls("/data", detail=True)

        assert result == entries
        mock_fs.ls.assert_awaited_once_with("/data", detail=True)

    def test_stat_returns_dict(self) -> None:
        info = {"size": 42, "type": "file"}
        mock_fs = _make_mock_async_fs(stat=info)
        sync_fs = SyncNexusFS(mock_fs)

        result = sync_fs.stat("/s3/bucket/file.txt")

        assert result == info

    def test_delete(self) -> None:
        mock_fs = _make_mock_async_fs(delete=None)
        sync_fs = SyncNexusFS(mock_fs)

        sync_fs.delete("/tmp/gone.txt")

        mock_fs.delete.assert_awaited_once_with("/tmp/gone.txt")

    def test_mkdir_passes_parents_kwarg(self) -> None:
        mock_fs = _make_mock_async_fs(mkdir=None)
        sync_fs = SyncNexusFS(mock_fs)

        sync_fs.mkdir("/new/dir", parents=False)

        mock_fs.mkdir.assert_awaited_once_with("/new/dir", parents=False)

    def test_rename(self) -> None:
        mock_fs = _make_mock_async_fs(rename=None)
        sync_fs = SyncNexusFS(mock_fs)

        sync_fs.rename("/old", "/new")

        mock_fs.rename.assert_awaited_once_with("/old", "/new")

    def test_exists_true(self) -> None:
        mock_fs = _make_mock_async_fs(exists=True)
        sync_fs = SyncNexusFS(mock_fs)

        assert sync_fs.exists("/s3/bucket/file.txt") is True

    def test_copy(self) -> None:
        mock_fs = _make_mock_async_fs(copy={"etag": "new"})
        sync_fs = SyncNexusFS(mock_fs)

        result = sync_fs.copy("/src", "/dst")

        assert result == {"etag": "new"}
        mock_fs.copy.assert_awaited_once_with("/src", "/dst")

    def test_close_delegates_when_method_exists(self) -> None:
        mock_fs = _make_mock_async_fs(close=None)
        sync_fs = SyncNexusFS(mock_fs)

        sync_fs.close()

        mock_fs.close.assert_awaited_once()

    def test_close_skips_when_no_method(self) -> None:
        """If the async facade has no close(), SyncNexusFS.close() is a no-op."""
        mock_fs = AsyncMock(spec=[])  # spec=[] -> no attributes
        sync_fs = SyncNexusFS(mock_fs)

        sync_fs.close()  # should not raise


# ---------------------------------------------------------------------------
# 2. Existing event loop -- Jupyter simulation
# ---------------------------------------------------------------------------


class TestExistingEventLoop:
    """Simulate Jupyter: an event loop is already running.

    We launch an async context, then from inside it, run the sync wrapper
    on a worker thread via anyio.to_thread.run_sync.  This must not deadlock.
    """

    @pytest.mark.anyio
    async def test_read_from_worker_thread(self) -> None:
        mock_fs = _make_mock_async_fs(read=b"jupyter data")
        sync_fs = SyncNexusFS(mock_fs)

        result = await anyio.to_thread.run_sync(sync_fs.read, "/nb/cell.txt")

        assert result == b"jupyter data"
        mock_fs.read.assert_awaited_once_with("/nb/cell.txt")

    @pytest.mark.anyio
    async def test_write_from_worker_thread(self) -> None:
        mock_fs = _make_mock_async_fs(write={"etag": "jup"})
        sync_fs = SyncNexusFS(mock_fs)

        def _do_write() -> dict:
            return sync_fs.write("/nb/out.bin", b"output")

        result = await anyio.to_thread.run_sync(_do_write)

        assert result == {"etag": "jup"}


# ---------------------------------------------------------------------------
# 3. Concurrent threads
# ---------------------------------------------------------------------------


class TestConcurrentThreads:
    """Spawn multiple threads hitting the same SyncNexusFS instance."""

    def test_ten_threads_read_no_deadlock(self) -> None:
        mock_fs = _make_mock_async_fs(read=b"ok")
        sync_fs = SyncNexusFS(mock_fs)
        results: list[bytes | BaseException] = [b""] * 10

        def _worker(idx: int) -> None:
            try:
                results[idx] = sync_fs.read(f"/file_{idx}.txt")
            except BaseException as exc:
                results[idx] = exc

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for i, r in enumerate(results):
            assert r == b"ok", f"Thread {i} failed: {r!r}"

        assert mock_fs.read.await_count == 10


# ---------------------------------------------------------------------------
# 4. Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    """Async-side exceptions must surface directly, not wrapped in a portal error."""

    def test_file_not_found_propagates(self) -> None:
        mock_fs = AsyncMock()
        mock_fs.read.side_effect = NexusFileNotFoundError("/missing.txt")
        sync_fs = SyncNexusFS(mock_fs)

        with pytest.raises(NexusFileNotFoundError) as exc_info:
            sync_fs.read("/missing.txt")

        assert exc_info.value.path == "/missing.txt"
        assert exc_info.value.is_expected is True

    def test_generic_exception_propagates(self) -> None:
        mock_fs = AsyncMock()
        mock_fs.write.side_effect = RuntimeError("boom")
        sync_fs = SyncNexusFS(mock_fs)

        with pytest.raises(RuntimeError, match="boom"):
            sync_fs.write("/x", b"data")

    @pytest.mark.anyio
    async def test_error_propagates_through_worker_thread(self) -> None:
        """Even when called from a worker thread, the original error type survives."""
        mock_fs = AsyncMock()
        mock_fs.stat.side_effect = NexusFileNotFoundError("/ghost")
        sync_fs = SyncNexusFS(mock_fs)

        def _do_stat() -> None:
            sync_fs.stat("/ghost")

        with pytest.raises(NexusFileNotFoundError):
            await anyio.to_thread.run_sync(_do_stat)
