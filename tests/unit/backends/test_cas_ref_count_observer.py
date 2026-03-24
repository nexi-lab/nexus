"""Unit tests for CASRefCountObserver (Issue #1320, #1748).

Verifies that the observer correctly calls release_content() on:
- FILE_WRITE with old_etag != new etag (overwrite)
- FILE_DELETE with etag (deletion)
- Skips when old_etag == new etag (same content rewrite)
- Skips when old_etag is None (new file creation)
- event_mask filters to FILE_WRITE | FILE_DELETE only
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.backends.observers.cas_ref_count_observer import CASRefCountObserver
from nexus.core.file_events import FILE_EVENT_BIT, FileEvent, FileEventType


@pytest.fixture
def engine() -> MagicMock:
    mock = MagicMock()
    mock.release_content = MagicMock()
    return mock


@pytest.fixture
def observer(engine: MagicMock) -> CASRefCountObserver:
    return CASRefCountObserver(engine)


class TestWriteOverwrite:
    async def test_overwrite_releases_old_etag(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/file.txt",
            etag="new_hash",
            old_etag="old_hash",
        )
        await observer.on_mutation(event)
        engine.release_content.assert_called_once_with("old_hash")

    async def test_same_etag_no_release(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/file.txt",
            etag="same_hash",
            old_etag="same_hash",
        )
        await observer.on_mutation(event)
        engine.release_content.assert_not_called()

    async def test_new_file_no_release(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/file.txt",
            etag="new_hash",
            old_etag=None,
            is_new=True,
        )
        await observer.on_mutation(event)
        engine.release_content.assert_not_called()


class TestDelete:
    async def test_delete_releases_etag(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/file.txt",
            etag="deleted_hash",
        )
        await observer.on_mutation(event)
        engine.release_content.assert_called_once_with("deleted_hash")

    async def test_delete_no_etag_no_release(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/dir",
            etag=None,
        )
        await observer.on_mutation(event)
        engine.release_content.assert_not_called()


class TestFaultIsolation:
    async def test_release_exception_does_not_propagate(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        engine.release_content.side_effect = RuntimeError("backend down")
        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/file.txt",
            etag="hash123",
        )
        # Should not raise — observer catches and logs
        await observer.on_mutation(event)

    async def test_unrelated_event_ignored(
        self, observer: CASRefCountObserver, engine: MagicMock
    ) -> None:
        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/old.txt",
            new_path="/new.txt",
        )
        await observer.on_mutation(event)
        engine.release_content.assert_not_called()


class TestEventMask:
    def test_event_mask_is_write_and_delete(self) -> None:
        expected = (
            FILE_EVENT_BIT[FileEventType.FILE_WRITE] | FILE_EVENT_BIT[FileEventType.FILE_DELETE]
        )
        assert CASRefCountObserver.event_mask == expected
