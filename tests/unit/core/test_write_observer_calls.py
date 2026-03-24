"""Unit tests verifying KernelDispatch notification for each mutation path.

Coverage matrix (all gaps closed in Issue #625, migrated to KernelDispatch in #900):
- write()       -> _dispatch.notify(FILE_WRITE)     YES
- delete()      -> _dispatch.notify(FILE_DELETE)     YES
- rename()      -> _dispatch.notify(FILE_RENAME)     YES
- write_batch() -> _dispatch.notify(FILE_WRITE) x N  YES
- write_stream()-> _dispatch.intercept_post_write()  YES
- mkdir()       -> _dispatch.notify(DIR_CREATE)      YES  (Issue #625)
- rmdir()       -> _dispatch.notify(DIR_DELETE)      YES  (Issue #625)

Phase 1.3 of #1246/#1330 consolidation plan.
Issue #900: Migrated from _write_observer to KernelDispatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.file_events import FileEventType
from tests.conftest import make_test_nexus

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS


@pytest.fixture
async def nx(tmp_path: Path) -> NexusFS:
    return await make_test_nexus(tmp_path)


@pytest.fixture
def mock_notify(nx: NexusFS) -> AsyncMock:
    """Replace _dispatch with a mock and return the mock's .notify attribute."""
    mock_dispatch = MagicMock()
    # resolve_* methods must return (handled=False, None) so sys_ methods
    # fall through to the real implementation instead of unpacking a MagicMock.
    mock_dispatch.resolve_read.return_value = (False, None)
    mock_dispatch.resolve_write.return_value = (False, None)
    mock_dispatch.resolve_delete.return_value = (False, None)
    # All post-dispatch and notify methods are now async
    mock_dispatch.notify = AsyncMock()
    mock_dispatch.intercept_post_write = AsyncMock()
    mock_dispatch.intercept_post_delete = AsyncMock()
    mock_dispatch.intercept_post_rename = AsyncMock()
    mock_dispatch.intercept_post_mkdir = AsyncMock()
    mock_dispatch.intercept_post_rmdir = AsyncMock()
    mock_dispatch.intercept_post_write_batch = AsyncMock()
    nx._dispatch = mock_dispatch
    return mock_dispatch.notify


# =========================================================================
# Operations that DO call _dispatch.notify()
# =========================================================================


class TestWriteCallsDispatch:
    """write() should call _dispatch.notify() with FILE_WRITE event."""

    @pytest.mark.asyncio
    async def test_new_file_notifies_with_is_new_true(
        self, nx: NexusFS, mock_notify: MagicMock
    ) -> None:
        await nx.write("/new.txt", b"hello")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/new.txt"
        assert event.is_new is True

    @pytest.mark.asyncio
    async def test_update_file_notifies_with_is_new_false(
        self, nx: NexusFS, mock_notify: MagicMock
    ) -> None:
        await nx.write("/file.txt", b"v1")
        mock_notify.reset_mock()

        await nx.write("/file.txt", b"v2")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.is_new is False
        assert event.path == "/file.txt"

    @pytest.mark.asyncio
    async def test_notify_receives_etag_and_size(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        await nx.write("/test.txt", b"content")

        event = mock_notify.call_args.args[0]
        assert event.path == "/test.txt"
        assert event.size == len(b"content")
        assert event.etag is not None


class TestDeleteCallsDispatch:
    """delete() should call _dispatch.notify() with FILE_DELETE event."""

    @pytest.mark.asyncio
    async def test_delete_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        await nx.write("/test.txt", b"content")
        mock_notify.reset_mock()

        await nx.sys_unlink("/test.txt")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_DELETE
        assert event.path == "/test.txt"

    @pytest.mark.asyncio
    async def test_delete_passes_etag(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        result = await nx.write("/test.txt", b"content")
        etag = result["etag"]
        mock_notify.reset_mock()

        await nx.sys_unlink("/test.txt")

        event = mock_notify.call_args.args[0]
        assert event.etag == etag


class TestRenameCallsDispatch:
    """rename() should call _dispatch.notify() with FILE_RENAME event."""

    @pytest.mark.asyncio
    async def test_rename_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        await nx.write("/old.txt", b"content")
        mock_notify.reset_mock()

        await nx.sys_rename("/old.txt", "/new.txt")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_RENAME
        assert event.path == "/old.txt"
        assert event.new_path == "/new.txt"


class TestWriteBatchCallsDispatch:
    """write_batch() should call _dispatch.notify() for each file."""

    @pytest.mark.asyncio
    async def test_batch_notifies_per_file(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb")]
        await nx.write_batch(files)

        assert mock_notify.call_count == 2
        paths = {call.args[0].path for call in mock_notify.call_args_list}
        assert paths == {"/a.txt", "/b.txt"}

    @pytest.mark.asyncio
    async def test_batch_events_are_file_write(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        files = [("/a.txt", b"aaa")]
        await nx.write_batch(files)

        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/a.txt"
        assert event.is_new is True


# =========================================================================
# write_stream, mkdir, rmdir — gaps closed (Issue #625)
# =========================================================================


class TestWriteStreamCallsDispatch:
    """write_stream() calls _dispatch.intercept_post_write() directly."""

    @pytest.mark.asyncio
    async def test_write_stream_calls_intercept(self, nx: NexusFS) -> None:
        if not hasattr(nx, "write_stream"):
            pytest.skip("write_stream not available")

        mock_dispatch = MagicMock()
        mock_dispatch.resolve_read.return_value = (False, None)
        mock_dispatch.resolve_write.return_value = (False, None)
        mock_dispatch.resolve_delete.return_value = (False, None)
        # All post-dispatch methods are now async
        mock_dispatch.intercept_post_write = AsyncMock()
        mock_dispatch.notify = AsyncMock()
        nx._dispatch = mock_dispatch

        # path_local backend requires backend_path in OperationContext for
        # streaming writes (no content_id available until hash is computed).
        from nexus.contracts.types import OperationContext

        ctx = OperationContext(user_id="test", groups=[], backend_path="streamed.txt")
        await nx.write_stream("/streamed.txt", iter([b"chunk1", b"chunk2"]), context=ctx)

        mock_dispatch.intercept_post_write.assert_called_once()
        hook_ctx = mock_dispatch.intercept_post_write.call_args.args[0]
        assert hook_ctx.path == "/streamed.txt"
        assert hook_ctx.is_new_file is True


class TestMkdirCallsDispatch:
    """mkdir() calls _dispatch.notify() with DIR_CREATE (Issue #625 gap closed)."""

    @pytest.mark.asyncio
    async def test_mkdir_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        await nx.mkdir("/testdir")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.DIR_CREATE
        assert event.path == "/testdir"

    @pytest.mark.asyncio
    async def test_mkdir_parents_notifies_dispatch(
        self, nx: NexusFS, mock_notify: MagicMock
    ) -> None:
        await nx.mkdir("/a/b/c", parents=True)

        # notify is called once for the final directory
        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.DIR_CREATE
        assert event.path == "/a/b/c"


class TestRmdirCallsDispatch:
    """rmdir() calls _dispatch.notify() with DIR_DELETE (Issue #625 gap closed)."""

    @pytest.mark.asyncio
    async def test_rmdir_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        await nx.mkdir("/mydir")
        mock_notify.reset_mock()

        await nx.sys_rmdir("/mydir")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.DIR_DELETE
        assert event.path == "/mydir"

    @pytest.mark.asyncio
    async def test_rmdir_recursive_notifies_dispatch(self, tmp_path: Path) -> None:
        """Use CAS backend to avoid PathLocal rmdir ordering bug (deletes dir marker before contents)."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        backend = CASLocalBackend(root_path=str(tmp_path / "cas_data"))
        cas_nx = await make_test_nexus(tmp_path, backend=backend)

        mock_dispatch = MagicMock()
        mock_dispatch.resolve_read.return_value = (False, None)
        mock_dispatch.resolve_write.return_value = (False, None)
        mock_dispatch.resolve_delete.return_value = (False, None)
        mock_dispatch.notify = AsyncMock()
        mock_dispatch.intercept_post_write = AsyncMock()
        mock_dispatch.intercept_post_delete = AsyncMock()
        mock_dispatch.intercept_post_rename = AsyncMock()
        mock_dispatch.intercept_post_mkdir = AsyncMock()
        mock_dispatch.intercept_post_rmdir = AsyncMock()
        mock_dispatch.intercept_post_write_batch = AsyncMock()
        cas_nx._dispatch = mock_dispatch
        mock_notify = mock_dispatch.notify

        await cas_nx.mkdir("/mydir")
        await cas_nx.write("/mydir/file.txt", b"content")
        mock_notify.reset_mock()

        await cas_nx.sys_rmdir("/mydir", recursive=True)

        # rmdir notify is the last call; write_batch notify may precede it
        events = [call.args[0] for call in mock_notify.call_args_list]
        rmdir_events = [e for e in events if e.type == FileEventType.DIR_DELETE]
        assert len(rmdir_events) == 1
        assert rmdir_events[0].path == "/mydir"


# =========================================================================
# Post-mutation hook coverage (Issue #625)
# =========================================================================


class TestVFSObserverCoverage:
    """Verify KernelDispatch OBSERVE fires for all mutation operations."""

    @pytest.fixture
    def hook(self) -> AsyncMock:
        """Async observer mock — on_mutation must be async."""
        mock = AsyncMock()
        mock.event_mask = (1 << 10) - 1  # ALL_FILE_EVENTS
        return mock

    @pytest.fixture
    async def nx_with_hook(self, tmp_path: Path, hook: AsyncMock) -> NexusFS:
        nx = await make_test_nexus(tmp_path)
        nx.register_observe(hook)
        return nx

    @pytest.mark.asyncio
    async def test_write_fires_hook(self, nx_with_hook: NexusFS, hook: AsyncMock) -> None:
        await nx_with_hook.write("/file.txt", b"hello")
        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/file.txt"

    @pytest.mark.asyncio
    async def test_delete_fires_hook(self, nx_with_hook: NexusFS, hook: AsyncMock) -> None:
        await nx_with_hook.write("/file.txt", b"hello")

        hook.reset_mock()
        await nx_with_hook.sys_unlink("/file.txt")

        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.type == FileEventType.FILE_DELETE

    @pytest.mark.asyncio
    async def test_rename_fires_hook(self, nx_with_hook: NexusFS, hook: AsyncMock) -> None:
        await nx_with_hook.write("/old.txt", b"hello")

        hook.reset_mock()
        await nx_with_hook.sys_rename("/old.txt", "/new.txt")

        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.type == FileEventType.FILE_RENAME
        assert event.new_path == "/new.txt"

    @pytest.mark.asyncio
    async def test_write_batch_fires_hook_per_file(
        self, nx_with_hook: NexusFS, hook: AsyncMock
    ) -> None:
        files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb"), ("/c.txt", b"ccc")]
        await nx_with_hook.write_batch(files)

        assert hook.on_mutation.call_count == 3
        paths = {call.args[0].path for call in hook.on_mutation.call_args_list}
        assert paths == {"/a.txt", "/b.txt", "/c.txt"}

    @pytest.mark.asyncio
    async def test_mkdir_fires_hook(self, nx_with_hook: NexusFS, hook: AsyncMock) -> None:
        await nx_with_hook.mkdir("/newdir")

        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.type == FileEventType.DIR_CREATE
        assert event.path == "/newdir"

    @pytest.mark.asyncio
    async def test_rmdir_fires_hook(self, nx_with_hook: NexusFS, hook: AsyncMock) -> None:
        await nx_with_hook.mkdir("/mydir")

        hook.reset_mock()
        await nx_with_hook.sys_rmdir("/mydir")

        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.type == FileEventType.DIR_DELETE
        assert event.path == "/mydir"
