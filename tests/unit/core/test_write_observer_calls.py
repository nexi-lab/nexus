"""Unit tests verifying KernelDispatch notification for each mutation path.

Coverage matrix (all gaps closed in Issue #625, migrated to KernelDispatch in #900):
- write()       -> _dispatch.notify(FILE_WRITE)     YES
- delete()      -> _dispatch.notify(FILE_DELETE)     YES
- rename()      -> _dispatch.notify(FILE_RENAME)     YES
- write_batch() -> _dispatch.notify(FILE_WRITE) x N  YES
- write_stream()-> dispatch_post_hooks("write", ctx) YES
- mkdir()       -> _dispatch.notify(DIR_CREATE)      YES  (Issue #625)
- rmdir()       -> _dispatch.notify(DIR_DELETE)      YES  (Issue #625)

Phase 1.3 of #1246/#1330 consolidation plan.
Issue #900: Migrated from _write_observer to KernelDispatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from nexus.core.file_events import ALL_FILE_EVENTS, FileEvent, FileEventType
from tests.conftest import make_test_nexus

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS


@pytest.fixture
def nx(tmp_path: Path) -> NexusFS:
    from nexus import CASLocalBackend

    backend = CASLocalBackend(str(tmp_path / "data"))
    return make_test_nexus(tmp_path, backend=backend)


class _CapturingObserver:
    """Captures events via a real sync VFSObserver registration.

    §11 Phase 6: ``notify()`` is only called on hit=false fallback paths;
    hit=true syscalls dispatch OBSERVE through the Rust kernel's
    background ThreadPool. Mocking ``nx.notify`` directly misses the
    Rust path entirely. A registered observer sees events from both
    paths — use ``flush()`` to drain the Rust ThreadPool before
    assertions.
    """

    event_mask: int = ALL_FILE_EVENTS

    def __init__(self) -> None:
        self.calls: list[FileEvent] = []

    def on_mutation(self, event: Any) -> None:
        self.calls.append(event)

    def flush(self, nx: "NexusFS") -> None:
        """Drain the Rust kernel observer ThreadPool."""
        nx._kernel.flush_observers()

    def assert_called_once(self) -> None:
        assert len(self.calls) == 1, f"expected 1 event, got {len(self.calls)}"

    def reset(self) -> None:
        self.calls.clear()

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_event(self) -> FileEvent:
        assert self.calls, "observer was not called"
        return self.calls[-1]


@pytest.fixture
def mock_notify(nx: NexusFS) -> _CapturingObserver:
    """Register a real observer and return it as the event capture point.

    Drop-in replacement for the old ``AsyncMock`` fixture: exposes the
    same methods test cases use (``assert_called_once``, ``reset_mock``,
    ``call_args``, ``call_count``, ``call_args_list``) via a thin
    adapter, but captures events from both the Rust ThreadPool (Tier 1
    hit=true path) and the Python fallback (``notify()`` hit=false path).
    """
    nx.resolve_read = MagicMock(return_value=(False, None))
    nx.resolve_write = MagicMock(return_value=(False, None))
    nx.resolve_delete = MagicMock(return_value=(False, None))

    obs = _CapturingObserver()
    nx.register_observe(obs)

    # Adapter: make _CapturingObserver quack like a MagicMock for the
    # existing assertion patterns. `reset_mock()` and `.call_args.args[0]`
    # come from unittest.mock idioms.
    class _FlushingAdapter:
        def __init__(self, obs: _CapturingObserver, nx: NexusFS) -> None:
            self._obs = obs
            self._nx = nx

        def _drain(self) -> None:
            self._nx._kernel.flush_observers()

        def assert_called_once(self) -> None:
            self._drain()
            self._obs.assert_called_once()

        def reset_mock(self) -> None:
            self._drain()
            self._obs.reset()

        @property
        def call_args(self) -> Any:
            self._drain()
            ev = self._obs.last_event

            class _Args:
                args = (ev,)

            return _Args()

        @property
        def call_count(self) -> int:
            self._drain()
            return self._obs.call_count

        @property
        def call_args_list(self) -> list[Any]:
            self._drain()

            class _Call:
                def __init__(self, ev: FileEvent) -> None:
                    self.args = (ev,)

            return [_Call(ev) for ev in self._obs.calls]

    return _FlushingAdapter(obs, nx)


# =========================================================================
# Operations that DO call _dispatch.notify()
# =========================================================================


class TestWriteCallsDispatch:
    """write() should call _dispatch.notify() with FILE_WRITE event."""

    @pytest.mark.asyncio
    def test_new_file_notifies_with_is_new_true(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        nx.write("/new.txt", b"hello")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/new.txt"
        assert event.is_new is True

    @pytest.mark.asyncio
    def test_update_file_notifies_with_is_new_false(
        self, nx: NexusFS, mock_notify: MagicMock
    ) -> None:
        nx.write("/file.txt", b"v1")
        mock_notify.reset_mock()

        nx.write("/file.txt", b"v2")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.is_new is False
        assert event.path == "/file.txt"

    @pytest.mark.asyncio
    def test_notify_receives_etag_and_size(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        nx.write("/test.txt", b"content")

        event = mock_notify.call_args.args[0]
        assert event.path == "/test.txt"
        assert event.size == len(b"content")
        assert event.etag is not None


class TestDeleteCallsDispatch:
    """delete() should call _dispatch.notify() with FILE_DELETE event."""

    @pytest.mark.asyncio
    def test_delete_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        nx.write("/test.txt", b"content")
        mock_notify.reset_mock()

        nx.sys_unlink("/test.txt")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_DELETE
        assert event.path == "/test.txt"

    @pytest.mark.asyncio
    def test_delete_passes_etag(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        result = nx.write("/test.txt", b"content")
        etag = result["etag"]
        mock_notify.reset_mock()

        nx.sys_unlink("/test.txt")

        event = mock_notify.call_args.args[0]
        assert event.etag == etag


class TestRenameCallsDispatch:
    """rename() should call _dispatch.notify() with FILE_RENAME event."""

    @pytest.mark.asyncio
    def test_rename_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        nx.write("/old.txt", b"content")
        mock_notify.reset_mock()

        nx.sys_rename("/old.txt", "/new.txt")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_RENAME
        assert event.path == "/old.txt"
        assert event.new_path == "/new.txt"


class TestWriteBatchCallsDispatch:
    """write_batch() should call _dispatch.notify() for each file."""

    @pytest.mark.asyncio
    def test_batch_notifies_per_file(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb")]
        nx.write_batch(files)

        assert mock_notify.call_count == 2
        paths = {call.args[0].path for call in mock_notify.call_args_list}
        assert paths == {"/a.txt", "/b.txt"}

    @pytest.mark.asyncio
    def test_batch_events_are_file_write(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        files = [("/a.txt", b"aaa")]
        nx.write_batch(files)

        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.FILE_WRITE
        assert event.path == "/a.txt"
        assert event.is_new is True


# =========================================================================
# write_stream, mkdir, rmdir — gaps closed (Issue #625)
# =========================================================================


class TestWriteStreamCallsDispatch:
    """write_stream() delegates to write() which dispatches post-write hooks via Rust."""

    @pytest.mark.asyncio
    def test_write_stream_dispatches_post_hooks(self, nx: NexusFS) -> None:
        if not hasattr(nx, "write_stream"):
            pytest.skip("write_stream not available")

        nx.resolve_read = MagicMock(return_value=(False, None))
        nx.resolve_write = MagicMock(return_value=(False, None))
        nx.resolve_delete = MagicMock(return_value=(False, None))
        # write_stream delegates to write() which dispatches hooks via Rust.
        nx.register_observe(_CapturingObserver())

        # Register a sync hook to verify dispatch
        hook = MagicMock()
        hook.name = "test_hook"
        nx.register_intercept_write(hook)

        from nexus.contracts.types import OperationContext

        ctx = OperationContext(user_id="test", groups=[], backend_path="streamed.txt")
        nx.write_stream("/streamed.txt", iter([b"chunk1", b"chunk2"]), context=ctx)

        hook.on_post_write.assert_called_once()
        hook_ctx = hook.on_post_write.call_args.args[0]
        assert hook_ctx.path == "/streamed.txt"


class TestMkdirCallsDispatch:
    """mkdir() calls _dispatch.notify() with DIR_CREATE (Issue #625 gap closed)."""

    @pytest.mark.asyncio
    def test_mkdir_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        nx.mkdir("/testdir")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.DIR_CREATE
        assert event.path == "/testdir"

    @pytest.mark.asyncio
    def test_mkdir_parents_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        nx.mkdir("/a/b/c", parents=True)

        # notify is called once for the final directory
        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.DIR_CREATE
        assert event.path == "/a/b/c"


class TestRmdirCallsDispatch:
    """rmdir() calls _dispatch.notify() with DIR_DELETE (Issue #625 gap closed)."""

    @pytest.mark.asyncio
    def test_rmdir_notifies_dispatch(self, nx: NexusFS, mock_notify: MagicMock) -> None:
        nx.mkdir("/mydir")
        mock_notify.reset_mock()

        nx.rmdir("/mydir")

        mock_notify.assert_called_once()
        event = mock_notify.call_args.args[0]
        assert event.type == FileEventType.DIR_DELETE
        assert event.path == "/mydir"

    @pytest.mark.asyncio
    def test_rmdir_recursive_notifies_dispatch(self, tmp_path: Path) -> None:
        """Use CAS backend to avoid PathLocal rmdir ordering bug (deletes dir marker before contents)."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        backend = CASLocalBackend(root_path=str(tmp_path / "cas_data"))
        cas_nx = make_test_nexus(tmp_path, backend=backend)

        cas_nx.resolve_read = MagicMock(return_value=(False, None))
        cas_nx.resolve_write = MagicMock(return_value=(False, None))
        cas_nx.resolve_delete = MagicMock(return_value=(False, None))
        obs = _CapturingObserver()
        cas_nx.register_observe(obs)

        cas_nx.mkdir("/mydir")
        cas_nx.write("/mydir/file.txt", b"content")
        cas_nx._kernel.flush_observers()
        obs.reset()

        cas_nx.rmdir("/mydir", recursive=True)
        cas_nx._kernel.flush_observers()

        # rmdir notify is the last call; write_batch notify may precede it
        rmdir_events = [e for e in obs.calls if e.type == FileEventType.DIR_DELETE]
        assert len(rmdir_events) == 1
        assert rmdir_events[0].path == "/mydir"


# =========================================================================
# Post-mutation hook coverage (Issue #625)
# =========================================================================


class TestVFSObserverCoverage:
    """Verify OBSERVE phase fires for all mutation operations.

    §11 Phase 6: observer.on_mutation is sync (was async_def). Uses a
    sync observer and calls ``flush_observers()`` to drain the Rust
    ThreadPool before assertions.
    """

    @pytest.fixture
    def hook(self) -> _CapturingObserver:
        return _CapturingObserver()

    @pytest.fixture
    def nx_with_hook(self, tmp_path: Path, hook: _CapturingObserver) -> NexusFS:
        from nexus import CASLocalBackend

        backend = CASLocalBackend(str(tmp_path / "data"))
        nx = make_test_nexus(tmp_path, backend=backend)
        nx.register_observe(hook)
        return nx

    @pytest.mark.asyncio
    def test_write_fires_hook(self, nx_with_hook: NexusFS, hook: _CapturingObserver) -> None:
        nx_with_hook.write("/file.txt", b"hello")
        hook.flush(nx_with_hook)
        hook.assert_called_once()
        assert hook.last_event.type == FileEventType.FILE_WRITE
        assert hook.last_event.path == "/file.txt"

    @pytest.mark.asyncio
    def test_delete_fires_hook(self, nx_with_hook: NexusFS, hook: _CapturingObserver) -> None:
        nx_with_hook.write("/file.txt", b"hello")
        hook.flush(nx_with_hook)
        hook.reset()

        nx_with_hook.sys_unlink("/file.txt")
        hook.flush(nx_with_hook)

        hook.assert_called_once()
        assert hook.last_event.type == FileEventType.FILE_DELETE

    @pytest.mark.asyncio
    def test_rename_fires_hook(self, nx_with_hook: NexusFS, hook: _CapturingObserver) -> None:
        nx_with_hook.write("/old.txt", b"hello")
        hook.flush(nx_with_hook)
        hook.reset()

        nx_with_hook.sys_rename("/old.txt", "/new.txt")
        hook.flush(nx_with_hook)

        hook.assert_called_once()
        assert hook.last_event.type == FileEventType.FILE_RENAME
        assert hook.last_event.new_path == "/new.txt"

    @pytest.mark.asyncio
    def test_write_batch_fires_hook_per_file(
        self, nx_with_hook: NexusFS, hook: _CapturingObserver
    ) -> None:
        files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb"), ("/c.txt", b"ccc")]
        nx_with_hook.write_batch(files)
        hook.flush(nx_with_hook)

        assert hook.call_count == 3
        paths = {e.path for e in hook.calls}
        assert paths == {"/a.txt", "/b.txt", "/c.txt"}

    @pytest.mark.asyncio
    def test_mkdir_fires_hook(self, nx_with_hook: NexusFS, hook: _CapturingObserver) -> None:
        nx_with_hook.mkdir("/newdir")
        hook.flush(nx_with_hook)

        hook.assert_called_once()
        assert hook.last_event.type == FileEventType.DIR_CREATE
        assert hook.last_event.path == "/newdir"

    @pytest.mark.asyncio
    def test_rmdir_fires_hook(self, nx_with_hook: NexusFS, hook: _CapturingObserver) -> None:
        nx_with_hook.mkdir("/mydir")
        hook.flush(nx_with_hook)
        hook.reset()

        nx_with_hook.rmdir("/mydir")
        hook.flush(nx_with_hook)

        hook.assert_called_once()
        assert hook.last_event.type == FileEventType.DIR_DELETE
        assert hook.last_event.path == "/mydir"
