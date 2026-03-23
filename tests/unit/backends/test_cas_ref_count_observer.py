"""Unit tests for CASRefCountObserver (Issue #1320).

Verifies that the observer correctly calls release_content() on:
- FILE_WRITE with old_etag != new etag (overwrite)
- FILE_DELETE with etag (deletion)
- Skips when old_etag == new etag (same content rewrite)
- Skips when old_etag is None (new file creation)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.backends.observers.cas_ref_count_observer import CASRefCountObserver
from nexus.core.file_events import FileEvent, FileEventType


@pytest.fixture
def engine() -> MagicMock:
    mock = MagicMock()
    mock.release_content = MagicMock()
    return mock


@pytest.fixture
def observer(engine: MagicMock) -> CASRefCountObserver:
    return CASRefCountObserver(engine)


class TestWriteOverwrite:
    def test_overwrite_releases_old_etag(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/file.txt",
            etag="new_hash",
            old_etag="old_hash",
        )
        observer.on_mutation(event)
        engine.release_content.assert_called_once_with("old_hash")

    def test_same_etag_no_release(self, observer: CASRefCountObserver, engine: MagicMock) -> None:
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/file.txt",
            etag="same_hash",
            old_etag="same_hash",
        )
        observer.on_mutation(event)
        engine.release_content.assert_not_called()

    def test_new_file_no_release(self, observer: CASRefCountObserver, engine: MagicMock) -> None:
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/file.txt",
            etag="new_hash",
            old_etag=None,
            is_new=True,
        )
        observer.on_mutation(event)
        engine.release_content.assert_not_called()


class TestDelete:
    def test_delete_releases_etag(self, observer: CASRefCountObserver, engine: MagicMock) -> None:
        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/file.txt",
            etag="deleted_hash",
        )
        observer.on_mutation(event)
        engine.release_content.assert_called_once_with("deleted_hash")

    def test_delete_no_etag_no_release(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/dir",
            etag=None,
        )
        observer.on_mutation(event)
        engine.release_content.assert_not_called()


class TestFaultIsolation:
    def test_release_exception_does_not_propagate(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        engine.release_content.side_effect = RuntimeError("backend down")
        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/file.txt",
            etag="hash123",
        )
        # Should not raise — observer catches and logs
        observer.on_mutation(event)

    def test_unrelated_event_ignored(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/old.txt",
            new_path="/new.txt",
        )
        observer.on_mutation(event)
        engine.release_content.assert_not_called()
