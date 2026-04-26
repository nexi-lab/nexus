"""Tests for SyncNexusFS — the synchronous wrapper around a NexusFS kernel.

After SlimNexusFS was deleted, SyncNexusFS wraps the kernel directly and
delegates to its sync ``sys_*`` / public methods (with ``LOCAL_CONTEXT``).
The wrapper still keeps a ``PortalRunner`` for the few entry points that
may run a coroutine and to preserve the original ``with SyncNexusFS(...)``
contract.

Covers four scenarios:
1. Direct sync calls (normal Python script)
2. Existing event loop (Jupyter-style, run sync wrapper from a worker thread)
3. Concurrent threads (10 threads calling read() in parallel)
4. Error propagation (kernel-side exception surfaces unchanged)
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import anyio.to_thread
import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.fs._helpers import LOCAL_CONTEXT
from nexus.fs._sync import SyncNexusFS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_kernel(**overrides: object) -> MagicMock:
    """Build a MagicMock that mimics a NexusFS kernel with sync methods."""
    mock = MagicMock()
    for attr, value in overrides.items():
        getattr(mock, attr).return_value = value
    return mock


# ---------------------------------------------------------------------------
# 1. Direct sync calls
# ---------------------------------------------------------------------------


class TestNoEventLoop:
    """Call SyncNexusFS from a plain synchronous context (no running loop)."""

    def test_read_returns_bytes(self) -> None:
        kernel = _make_mock_kernel(sys_read=b"hello world")
        sync_fs = SyncNexusFS(kernel)

        result = sync_fs.read("/s3/bucket/file.txt")

        assert result == b"hello world"
        kernel.sys_read.assert_called_once_with("/s3/bucket/file.txt", context=LOCAL_CONTEXT)

    def test_write_returns_metadata(self) -> None:
        kernel = _make_mock_kernel(write={"etag": "abc123"})
        sync_fs = SyncNexusFS(kernel)

        result = sync_fs.write("/s3/bucket/out.bin", b"data")

        assert result == {"etag": "abc123"}
        kernel.write.assert_called_once_with("/s3/bucket/out.bin", b"data", context=LOCAL_CONTEXT)

    def test_ls_passes_detail_kwarg(self) -> None:
        entries = [{"name": "a.txt", "size": 10}]
        kernel = _make_mock_kernel(sys_readdir=entries)
        sync_fs = SyncNexusFS(kernel)

        result = sync_fs.ls("/data", detail=True)

        assert result == entries
        kernel.sys_readdir.assert_called_once_with(
            "/data", recursive=False, details=True, context=LOCAL_CONTEXT
        )

    def test_stat_returns_dict(self) -> None:
        info = {"size": 42, "type": "file"}
        kernel = _make_mock_kernel(sys_stat=info)
        sync_fs = SyncNexusFS(kernel)

        result = sync_fs.stat("/s3/bucket/file.txt")

        assert result == info

    def test_delete(self) -> None:
        kernel = _make_mock_kernel(sys_unlink=None)
        sync_fs = SyncNexusFS(kernel)

        sync_fs.delete("/tmp/gone.txt")

        kernel.sys_unlink.assert_called_once_with("/tmp/gone.txt", context=LOCAL_CONTEXT)

    def test_mkdir_passes_parents_kwarg(self) -> None:
        kernel = _make_mock_kernel(mkdir=None)
        sync_fs = SyncNexusFS(kernel)

        sync_fs.mkdir("/new/dir", parents=False)

        kernel.mkdir.assert_called_once_with(
            "/new/dir", parents=False, exist_ok=True, context=LOCAL_CONTEXT
        )

    def test_rename(self) -> None:
        kernel = _make_mock_kernel(sys_rename=None)
        sync_fs = SyncNexusFS(kernel)

        sync_fs.rename("/old", "/new")

        kernel.sys_rename.assert_called_once_with("/old", "/new", context=LOCAL_CONTEXT)

    def test_exists_true(self) -> None:
        kernel = _make_mock_kernel(access=True)
        sync_fs = SyncNexusFS(kernel)

        assert sync_fs.exists("/s3/bucket/file.txt") is True

    def test_copy(self) -> None:
        kernel = _make_mock_kernel(sys_copy={"etag": "new"})
        sync_fs = SyncNexusFS(kernel)

        result = sync_fs.copy("/src", "/dst")

        assert result == {"etag": "new"}
        kernel.sys_copy.assert_called_once_with("/src", "/dst", context=LOCAL_CONTEXT)

    def test_close_delegates_via_helper(self) -> None:
        kernel = _make_mock_kernel(close=None)
        sync_fs = SyncNexusFS(kernel)

        sync_fs.close()

        kernel.close.assert_called_once()
        # The metadata sub-store close runs through the helper too.
        kernel.metadata.close.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Existing event loop -- Jupyter simulation
# ---------------------------------------------------------------------------


class TestExistingEventLoop:
    """Simulate Jupyter: an event loop is already running.

    We launch an async context, then from inside it, run the sync wrapper
    on a worker thread via anyio.to_thread.run_sync.  This must not
    deadlock.
    """

    @pytest.mark.anyio
    async def test_read_from_worker_thread(self) -> None:
        kernel = _make_mock_kernel(sys_read=b"jupyter data")
        sync_fs = SyncNexusFS(kernel)

        result = await anyio.to_thread.run_sync(sync_fs.read, "/nb/cell.txt")

        assert result == b"jupyter data"
        kernel.sys_read.assert_called_once_with("/nb/cell.txt", context=LOCAL_CONTEXT)

    @pytest.mark.anyio
    async def test_write_from_worker_thread(self) -> None:
        kernel = _make_mock_kernel(write={"etag": "jup"})
        sync_fs = SyncNexusFS(kernel)

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
        kernel = _make_mock_kernel(sys_read=b"ok")
        sync_fs = SyncNexusFS(kernel)
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

        assert kernel.sys_read.call_count == 10


# ---------------------------------------------------------------------------
# 4. Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    """Kernel-side exceptions surface directly, untouched by the wrapper."""

    def test_file_not_found_propagates(self) -> None:
        kernel = MagicMock()
        kernel.sys_read.side_effect = NexusFileNotFoundError("/missing.txt")
        sync_fs = SyncNexusFS(kernel)

        with pytest.raises(NexusFileNotFoundError) as exc_info:
            sync_fs.read("/missing.txt")

        assert exc_info.value.path == "/missing.txt"
        assert exc_info.value.is_expected is True

    def test_generic_exception_propagates(self) -> None:
        kernel = MagicMock()
        kernel.write.side_effect = RuntimeError("boom")
        sync_fs = SyncNexusFS(kernel)

        with pytest.raises(RuntimeError, match="boom"):
            sync_fs.write("/x", b"data")

    @pytest.mark.anyio
    async def test_error_propagates_through_worker_thread(self) -> None:
        """Even when called from a worker thread, the original error type survives."""
        kernel = MagicMock()
        kernel.sys_stat.side_effect = NexusFileNotFoundError("/ghost")
        sync_fs = SyncNexusFS(kernel)

        def _do_stat() -> None:
            sync_fs.stat("/ghost")

        with pytest.raises(NexusFileNotFoundError):
            await anyio.to_thread.run_sync(_do_stat)
