"""Unit tests verifying _write_observer is called for each mutation path.

Coverage matrix (all gaps closed in Issue #625):
- write()       -> on_write()       YES
- delete()      -> on_delete()      YES
- rename()      -> on_rename()      YES
- write_batch() -> on_write_batch() YES
- write_stream()-> on_write()       YES
- mkdir()       -> on_mkdir()       YES  (Issue #625)
- rmdir()       -> on_rmdir()       YES  (Issue #625)

Phase 1.3 of #1246/#1330 consolidation plan.
Issue #2152: Observer owns error policy — kernel is a pure caller (no try/except).
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.config import ParseConfig, PermissionConfig, SystemServices
from tests.helpers.in_memory_metadata_store import InMemoryMetastore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def observer() -> MagicMock:
    """Non-failing mock observer to record calls."""
    return MagicMock()


@pytest.fixture
def nx(temp_dir: Path, observer: MagicMock) -> Generator[NexusFS, None, None]:
    nx = NexusFS(
        backend=LocalBackend(str(temp_dir / "data")),
        metadata_store=InMemoryMetastore(),
        permissions=PermissionConfig(enforce=False),
        parsing=ParseConfig(auto_parse=False),
        system_services=SystemServices(write_observer=observer),
    )

    # Wire PermissionChecker via DI (same as factory/orchestrator.py, Issue #874)
    from nexus.services.permissions.checker import PermissionChecker

    nx._permission_checker = PermissionChecker(
        permission_enforcer=nx._permission_enforcer,
        metadata_store=nx.metadata,
        default_context=nx._default_context,
        enforce_permissions=nx._enforce_permissions,
    )

    yield nx
    nx.close()


# =========================================================================
# Operations that DO call the observer
# =========================================================================


class TestWriteCallsObserver:
    """write() should call on_write() with correct arguments."""

    def test_new_file_calls_on_write_with_is_new_true(
        self, nx: NexusFS, observer: MagicMock
    ) -> None:
        nx.write("/new.txt", b"hello")

        observer.on_write.assert_called_once()
        kwargs = observer.on_write.call_args
        assert kwargs.kwargs["is_new"] is True
        assert kwargs.kwargs["path"] == "/new.txt"

    def test_update_file_calls_on_write_with_is_new_false(
        self, nx: NexusFS, observer: MagicMock
    ) -> None:
        nx.write("/file.txt", b"v1")
        observer.reset_mock()

        nx.write("/file.txt", b"v2")

        observer.on_write.assert_called_once()
        kwargs = observer.on_write.call_args
        assert kwargs.kwargs["is_new"] is False
        assert kwargs.kwargs["path"] == "/file.txt"

    def test_on_write_receives_metadata_object(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.write("/test.txt", b"content")

        call_kwargs = observer.on_write.call_args
        metadata = call_kwargs.kwargs.get("metadata") or call_kwargs.args[0]
        assert metadata.path == "/test.txt"
        assert metadata.size == len(b"content")
        assert metadata.etag is not None


class TestDeleteCallsObserver:
    """delete() should call on_delete() with correct arguments."""

    def test_delete_calls_on_delete(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.write("/test.txt", b"content")
        observer.reset_mock()

        nx.delete("/test.txt")

        observer.on_delete.assert_called_once()
        kwargs = observer.on_delete.call_args.kwargs
        assert kwargs["path"] == "/test.txt"

    def test_delete_passes_snapshot_hash(self, nx: NexusFS, observer: MagicMock) -> None:
        result = nx.write("/test.txt", b"content")
        etag = result["etag"]
        observer.reset_mock()

        nx.delete("/test.txt")

        kwargs = observer.on_delete.call_args.kwargs
        assert kwargs["snapshot_hash"] == etag


class TestRenameCallsObserver:
    """rename() should call on_rename() with correct arguments."""

    def test_rename_calls_on_rename(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.write("/old.txt", b"content")
        observer.reset_mock()

        nx.rename("/old.txt", "/new.txt")

        observer.on_rename.assert_called_once()
        kwargs = observer.on_rename.call_args.kwargs
        assert kwargs["old_path"] == "/old.txt"
        assert kwargs["new_path"] == "/new.txt"


class TestWriteBatchCallsObserver:
    """write_batch() should call on_write_batch() with correct arguments."""

    def test_batch_calls_on_write_batch(self, nx: NexusFS, observer: MagicMock) -> None:
        files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb")]
        nx.write_batch(files)

        observer.on_write_batch.assert_called_once()
        call_kwargs = observer.on_write_batch.call_args
        items = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs["items"]
        assert len(items) == 2

    def test_batch_items_contain_metadata_and_is_new(
        self, nx: NexusFS, observer: MagicMock
    ) -> None:
        files = [("/a.txt", b"aaa")]
        nx.write_batch(files)

        call_kwargs = observer.on_write_batch.call_args
        items = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs["items"]
        metadata, is_new = items[0]
        assert metadata.path == "/a.txt"
        assert is_new is True


# =========================================================================
# write_stream, mkdir, rmdir — gaps closed (Issue #625)
# =========================================================================


class TestWriteStreamCallsObserver:
    """write_stream() calls _write_observer.on_write() directly."""

    def test_write_stream_calls_on_write(self, nx: NexusFS, observer: MagicMock) -> None:
        if not hasattr(nx, "write_stream"):
            pytest.skip("write_stream not available")

        nx.write_stream("/streamed.txt", iter([b"chunk1", b"chunk2"]))

        observer.on_write.assert_called_once()
        kwargs = observer.on_write.call_args.kwargs
        assert kwargs["path"] == "/streamed.txt"
        assert kwargs["is_new"] is True


class TestMkdirCallsObserver:
    """mkdir() calls on_mkdir() (Issue #625 gap closed)."""

    def test_mkdir_calls_on_mkdir(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.mkdir("/testdir")

        observer.on_mkdir.assert_called_once()
        kwargs = observer.on_mkdir.call_args.kwargs
        assert kwargs["path"] == "/testdir"

    def test_mkdir_parents_calls_on_mkdir(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.mkdir("/a/b/c", parents=True)

        # on_mkdir is called once for the final directory
        observer.on_mkdir.assert_called_once()
        kwargs = observer.on_mkdir.call_args.kwargs
        assert kwargs["path"] == "/a/b/c"


class TestRmdirCallsObserver:
    """rmdir() calls on_rmdir() (Issue #625 gap closed)."""

    def test_rmdir_calls_on_rmdir(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.mkdir("/mydir")
        observer.reset_mock()

        nx.rmdir("/mydir")

        observer.on_rmdir.assert_called_once()
        kwargs = observer.on_rmdir.call_args.kwargs
        assert kwargs["path"] == "/mydir"

    def test_rmdir_recursive_calls_on_rmdir(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.mkdir("/mydir")
        nx.write("/mydir/file.txt", b"content")
        observer.reset_mock()

        nx.rmdir("/mydir", recursive=True)

        observer.on_rmdir.assert_called_once()
        kwargs = observer.on_rmdir.call_args.kwargs
        assert kwargs["path"] == "/mydir"
        assert kwargs["recursive"] is True


# =========================================================================
# Post-mutation hook coverage (Issue #625)
# =========================================================================


class TestPostMutationHookCoverage:
    """Verify _fire_post_mutation_hooks fires for all mutation operations."""

    @pytest.fixture
    def hook(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def nx_with_hook(
        self, temp_dir: Path, observer: MagicMock, hook: MagicMock
    ) -> Generator[NexusFS, None, None]:
        nx = NexusFS(
            backend=LocalBackend(str(temp_dir / "data")),
            metadata_store=InMemoryMetastore(),
            permissions=PermissionConfig(enforce=False),
            parsing=ParseConfig(auto_parse=False),
            system_services=SystemServices(write_observer=observer),
        )

        # Wire PermissionChecker via DI (same as factory/orchestrator.py, Issue #874)
        from nexus.services.permissions.checker import PermissionChecker

        nx._permission_checker = PermissionChecker(
            permission_enforcer=nx._permission_enforcer,
            metadata_store=nx.metadata,
            default_context=nx._default_context,
            enforce_permissions=nx._enforce_permissions,
        )

        nx.register_mutation_hook(hook)
        yield nx
        nx.close()

    def test_write_fires_hook(self, nx_with_hook: NexusFS, hook: MagicMock) -> None:
        nx_with_hook.write("/file.txt", b"hello")
        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.operation.value == "write"
        assert event.path == "/file.txt"

    def test_delete_fires_hook(self, nx_with_hook: NexusFS, hook: MagicMock) -> None:
        nx_with_hook.write("/file.txt", b"hello")
        hook.reset_mock()
        nx_with_hook.delete("/file.txt")
        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.operation.value == "delete"

    def test_rename_fires_hook(self, nx_with_hook: NexusFS, hook: MagicMock) -> None:
        nx_with_hook.write("/old.txt", b"hello")
        hook.reset_mock()
        nx_with_hook.rename("/old.txt", "/new.txt")
        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.operation.value == "rename"
        assert event.new_path == "/new.txt"

    def test_write_batch_fires_hook_per_file(self, nx_with_hook: NexusFS, hook: MagicMock) -> None:
        files = [("/a.txt", b"aaa"), ("/b.txt", b"bbb"), ("/c.txt", b"ccc")]
        nx_with_hook.write_batch(files)
        assert hook.on_mutation.call_count == 3
        paths = {call.args[0].path for call in hook.on_mutation.call_args_list}
        assert paths == {"/a.txt", "/b.txt", "/c.txt"}

    def test_mkdir_fires_hook(self, nx_with_hook: NexusFS, hook: MagicMock) -> None:
        nx_with_hook.mkdir("/newdir")
        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.operation.value == "mkdir"
        assert event.path == "/newdir"

    def test_rmdir_fires_hook(self, nx_with_hook: NexusFS, hook: MagicMock) -> None:
        nx_with_hook.mkdir("/mydir")
        hook.reset_mock()
        nx_with_hook.rmdir("/mydir")
        hook.on_mutation.assert_called_once()
        event = hook.on_mutation.call_args.args[0]
        assert event.operation.value == "rmdir"
        assert event.path == "/mydir"


# =========================================================================
# Observer error handling (Issue #1631)
# =========================================================================


def _make_failing_observer() -> MagicMock:
    """Create a mock observer where every method raises RuntimeError."""
    obs = MagicMock()
    err = RuntimeError("observer boom")
    obs.on_write.side_effect = err
    obs.on_write_batch.side_effect = err
    obs.on_delete.side_effect = err
    obs.on_rename.side_effect = err
    obs.on_mkdir.side_effect = err
    obs.on_rmdir.side_effect = err
    return obs


class TestObserverErrorPropagation:
    """Issue #2152: Observer errors propagate directly (no kernel safety net)."""

    @pytest.fixture
    def failing_observer(self) -> MagicMock:
        return _make_failing_observer()

    @pytest.fixture
    def nx_with_failing_observer(
        self, temp_dir: Path, failing_observer: MagicMock
    ) -> Generator[NexusFS, None, None]:
        nx = NexusFS(
            backend=LocalBackend(str(temp_dir / "data")),
            metadata_store=InMemoryMetastore(),
            permissions=PermissionConfig(enforce=False),
            parsing=ParseConfig(auto_parse=False),
            system_services=SystemServices(write_observer=failing_observer),
        )

        # Wire PermissionChecker via DI (same as factory/orchestrator.py, Issue #874)
        from nexus.services.permissions.checker import PermissionChecker

        nx._permission_checker = PermissionChecker(
            permission_enforcer=nx._permission_enforcer,
            metadata_store=nx.metadata,
            default_context=nx._default_context,
            enforce_permissions=nx._enforce_permissions,
        )

        yield nx
        nx.close()

    def test_write_propagates_observer_error(self, nx_with_failing_observer: NexusFS) -> None:
        """Observer RuntimeError propagates — no kernel try/except wrapper."""
        with pytest.raises(RuntimeError, match="observer boom"):
            nx_with_failing_observer.write("/test.txt", b"hello")

    def test_mkdir_propagates_observer_error(self, nx_with_failing_observer: NexusFS) -> None:
        with pytest.raises(RuntimeError, match="observer boom"):
            nx_with_failing_observer.mkdir("/testdir")

    def test_rmdir_propagates_observer_error(
        self, nx_with_failing_observer: NexusFS, failing_observer: MagicMock
    ) -> None:
        # Create dir with a non-failing observer first, then switch
        failing_observer.on_mkdir.side_effect = None
        nx_with_failing_observer.mkdir("/testdir")
        failing_observer.on_rmdir.side_effect = RuntimeError("observer boom")

        with pytest.raises(RuntimeError, match="observer boom"):
            nx_with_failing_observer.rmdir("/testdir")
