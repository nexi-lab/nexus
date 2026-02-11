"""Unit tests verifying _write_observer is called (or not) for each write path.

Documents and tests which operations currently call the observer:
- write()       -> on_write()       YES
- delete()      -> on_delete()      YES
- rename()      -> on_rename()      YES
- write_batch() -> on_write_batch() YES
- write_stream()-> (none)           NO  <-- gap
- mkdir()       -> (none)           NO  <-- gap
- rmdir()       -> (none)           NO  <-- gap

Phase 1.3 of #1246/#1330 consolidation plan.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus import LocalBackend, NexusFS
from tests.helpers.in_memory_metadata_store import InMemoryFileMetadataStore


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
        metadata_store=InMemoryFileMetadataStore(),
        enforce_permissions=False,
        auto_parse=False,
        write_observer=observer,
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
# Operations that do NOT call the observer (documenting current gaps)
# =========================================================================


class TestWriteStreamCallsObserver:
    """write_stream() now calls on_write() via _notify_observer (gap closed)."""

    def test_write_stream_calls_on_write(self, nx: NexusFS, observer: MagicMock) -> None:
        if not hasattr(nx, "write_stream"):
            pytest.skip("write_stream not available")

        nx.write_stream("/streamed.txt", iter([b"chunk1", b"chunk2"]))

        observer.on_write.assert_called_once()
        kwargs = observer.on_write.call_args.kwargs
        assert kwargs["path"] == "/streamed.txt"
        assert kwargs["is_new"] is True


class TestMkdirDoesNotCallObserver:
    """mkdir() currently does NOT call any observer method."""

    def test_mkdir_skips_observer(self, nx: NexusFS, observer: MagicMock) -> None:
        nx.mkdir("/testdir")

        # Document the gap: observer should be called but isn't
        observer.on_write.assert_not_called()


class TestRmdirDoesNotCallObserver:
    """rmdir() currently does NOT call any observer method."""

    def test_rmdir_skips_observer(self, nx: NexusFS, observer: MagicMock) -> None:
        # Create a directory and a file in it
        nx.mkdir("/mydir")
        observer.reset_mock()

        nx.rmdir("/mydir")

        # Document the gap: observer should be called but isn't
        observer.on_delete.assert_not_called()
