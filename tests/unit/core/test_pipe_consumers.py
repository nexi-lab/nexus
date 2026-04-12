"""Integration tests: OBSERVE-phase observers end-to-end (#926, #809, #810).

Verifies that:
- ZoektWriteObserver receives FileEvents via on_mutation (OBSERVE phase)
  and triggers Zoekt reindex after debounce.
- RecordStoreWriteObserver receives FileEvents via on_mutation (OBSERVE phase)
  and flushes audit events to RecordStore after debounce.

See: factory/zoekt_observer.py, storage/piped_record_store_write_observer.py
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

# ======================================================================
# ZoektWriteObserver — OBSERVE-phase tests
# ======================================================================


def _make_file_event(path: str) -> "FileEvent":  # noqa: F821
    """Create a FILE_WRITE FileEvent for testing."""
    from nexus.core.file_events import FileEvent, FileEventType

    return FileEvent(type=FileEventType.FILE_WRITE, path=path)


class TestZoektWriteObserver:
    """Prove OBSERVE phase: on_mutation(FileEvent) -> debounce -> reindex."""

    def test_on_mutation_triggers_reindex(self) -> None:
        """on_mutation accumulates paths and triggers reindex after debounce."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05
        zoekt.trigger_reindex = MagicMock()

        observer = ZoektWriteObserver(zoekt, debounce_seconds=0.05)

        observer.on_mutation(_make_file_event("/workspace/file1.txt"))
        observer.on_mutation(_make_file_event("/workspace/file2.txt"))

        # Wait for debounce to fire
        time.sleep(0.15)

        zoekt.trigger_reindex.assert_called_once()
        observer.cancel()

    def test_debounce_coalesces_writes(self) -> None:
        """Multiple rapid writes within debounce window -> single reindex."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.1
        zoekt.trigger_reindex = MagicMock()

        observer = ZoektWriteObserver(zoekt, debounce_seconds=0.1)

        # Rapid-fire 20 writes within debounce window
        for i in range(20):
            observer.on_mutation(_make_file_event(f"/workspace/file{i}.txt"))

        # Wait for debounce to fire
        time.sleep(0.25)

        # Should coalesce into 1 reindex call (not 20)
        assert zoekt.trigger_reindex.call_count == 1
        observer.cancel()

    def test_debounce_resets_on_new_write(self) -> None:
        """A new write resets the debounce timer."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.15
        zoekt.trigger_reindex = MagicMock()

        observer = ZoektWriteObserver(zoekt, debounce_seconds=0.15)

        observer.on_mutation(_make_file_event("/workspace/file1.txt"))
        time.sleep(0.08)  # < debounce window
        observer.on_mutation(_make_file_event("/workspace/file2.txt"))  # resets timer
        time.sleep(0.08)  # still < debounce from last write

        # Should NOT have fired yet (timer was reset)
        assert zoekt.trigger_reindex.call_count == 0

        # Wait for full debounce after last write
        time.sleep(0.15)
        assert zoekt.trigger_reindex.call_count == 1
        observer.cancel()

    def test_event_mask_is_file_write(self) -> None:
        """event_mask only includes FILE_WRITE."""
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 1.0

        observer = ZoektWriteObserver(zoekt)
        assert observer.event_mask == FILE_EVENT_BIT[FileEventType.FILE_WRITE]
        observer.cancel()

    def test_hook_spec_returns_observer(self) -> None:
        """hook_spec() returns HookSpec with self as observer."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 1.0

        observer = ZoektWriteObserver(zoekt)
        spec = observer.hook_spec()
        assert observer in spec.observers
        observer.cancel()

    def test_legacy_notify_write_fallback(self) -> None:
        """notify_write() falls back to direct ZoektIndexManager.notify_write()."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05

        observer = ZoektWriteObserver(zoekt)

        observer.notify_write("/workspace/file.txt")
        zoekt.notify_write.assert_called_once_with("/workspace/file.txt")
        observer.cancel()

    def test_legacy_notify_sync_complete_fallback(self) -> None:
        """notify_sync_complete() falls back to direct ZoektIndexManager call."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05

        observer = ZoektWriteObserver(zoekt)

        observer.notify_sync_complete(files_synced=5)
        zoekt.notify_sync_complete.assert_called_once_with(5)
        observer.cancel()

    def test_cancel_stops_pending_timer(self) -> None:
        """cancel() stops pending debounce timer -- no reindex after cancel."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.2
        zoekt.trigger_reindex = MagicMock()

        observer = ZoektWriteObserver(zoekt, debounce_seconds=0.2)

        observer.on_mutation(_make_file_event("/workspace/file.txt"))
        observer.cancel()

        # Wait past what would have been the debounce window
        time.sleep(0.35)

        # Reindex should NOT have been called (timer cancelled)
        assert zoekt.trigger_reindex.call_count == 0

    def test_paths_accumulated_and_cleared(self) -> None:
        """Pending paths are accumulated and cleared after flush."""
        from nexus.factory.zoekt_observer import ZoektWriteObserver

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05
        zoekt.trigger_reindex = MagicMock()

        observer = ZoektWriteObserver(zoekt, debounce_seconds=0.05)

        observer.on_mutation(_make_file_event("/workspace/a.txt"))
        observer.on_mutation(_make_file_event("/workspace/b.txt"))
        observer.on_mutation(_make_file_event("/workspace/a.txt"))  # dedup

        # Check internal state before flush
        assert len(observer._pending) == 2

        # Wait for flush
        time.sleep(0.15)

        assert len(observer._pending) == 0
        zoekt.trigger_reindex.assert_called_once()
        observer.cancel()


# ======================================================================
# RecordStoreWriteObserver (OBSERVE-phase) — end-to-end tests
# ======================================================================


def _make_write_file_event(path: str, *, is_new: bool = True, size: int = 100) -> "FileEvent":  # noqa: F821
    """Create a FILE_WRITE FileEvent for testing."""
    from nexus.core.file_events import FileEvent, FileEventType

    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path=path,
        is_new=is_new,
        size=size,
        etag=f"hash_{path}",
    )


def _make_delete_file_event(path: str) -> "FileEvent":  # noqa: F821
    """Create a FILE_DELETE FileEvent for testing."""
    from nexus.core.file_events import FileEvent, FileEventType

    return FileEvent(type=FileEventType.FILE_DELETE, path=path)


def _make_dir_create_event(path: str) -> "FileEvent":  # noqa: F821
    """Create a DIR_CREATE FileEvent for testing."""
    from nexus.core.file_events import FileEvent, FileEventType

    return FileEvent(type=FileEventType.DIR_CREATE, path=path)


def _make_dir_delete_event(path: str) -> "FileEvent":  # noqa: F821
    """Create a DIR_DELETE FileEvent for testing."""
    from nexus.core.file_events import FileEvent, FileEventType

    return FileEvent(type=FileEventType.DIR_DELETE, path=path)


def _noop_process_events(session: object, events: list[dict[str, object]]) -> None:
    """No-op replacement for _process_events_in_session (avoids sqlalchemy dep)."""


class TestRecordStoreWriteObserverE2E:
    """Prove OBSERVE phase: on_mutation(FileEvent) -> debounce -> flush to RecordStore.

    We patch ``_process_events_in_session`` (the DB flush layer) so these tests
    verify the OBSERVE callback path without requiring sqlalchemy/RecordStore.
    """

    def test_write_event_flows_through_observer(self) -> None:
        """Full E2E: on_mutation -> debounce -> _flush_batch."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=0.05)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            observer.on_mutation(_make_write_file_event("/workspace/test.txt"))
            # Wait for debounce to fire
            time.sleep(0.2)
            assert observer._total_flushed >= 1
        observer.cancel()

    def test_batch_write_flows_through_observer(self) -> None:
        """Multiple events coalesce into one debounced flush."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=0.05)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            for i in range(5):
                observer.on_mutation(_make_write_file_event(f"/workspace/file{i}.txt", size=i * 10))
            time.sleep(0.2)
            assert observer._total_flushed >= 5
        observer.cancel()

    def test_delete_event_flows_through_observer(self) -> None:
        """Delete event flows through observer to flush."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=0.05)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            observer.on_mutation(_make_delete_file_event("/workspace/deleted.txt"))
            time.sleep(0.2)
            assert observer._total_flushed >= 1
        observer.cancel()

    def test_flush_sync_drains_pending(self) -> None:
        """flush_sync() drains pending events directly to DB."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_record_store = MagicMock()
        mock_record_store.session_factory.return_value = mock_session

        observer = RecordStoreWriteObserver(mock_record_store, debounce_seconds=10.0)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            # Feed events via on_mutation (timer won't fire — 10s debounce)
            observer.on_mutation(_make_write_file_event("/workspace/cli.txt"))
            observer.on_mutation(_make_delete_file_event("/workspace/old.txt"))

            assert len(observer._pending) == 2

            # flush_sync drains immediately
            flushed = observer.flush_sync()

        assert flushed == 2
        assert observer._total_flushed == 2
        assert len(observer._pending) == 0
        observer.cancel()

    def test_metrics_tracking(self) -> None:
        """Observer metrics reflect actual event flow."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=0.05)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            observer.on_mutation(_make_write_file_event("/workspace/metrics.txt"))
            observer.on_mutation(_make_dir_create_event("/workspace/newdir"))
            observer.on_mutation(_make_dir_delete_event("/workspace/olddir"))

            time.sleep(0.2)

            metrics = observer.metrics
            assert metrics["total_flushed"] >= 3
            assert metrics["total_failed"] == 0
            assert metrics["total_dropped"] == 0
        observer.cancel()

    def test_event_mask_covers_mutations(self) -> None:
        """event_mask includes all mutation event types."""
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock())
        mask = observer.event_mask
        assert mask & FILE_EVENT_BIT[FileEventType.FILE_WRITE]
        assert mask & FILE_EVENT_BIT[FileEventType.FILE_DELETE]
        assert mask & FILE_EVENT_BIT[FileEventType.FILE_RENAME]
        assert mask & FILE_EVENT_BIT[FileEventType.DIR_CREATE]
        assert mask & FILE_EVENT_BIT[FileEventType.DIR_DELETE]
        observer.cancel()

    def test_hook_spec_returns_observer(self) -> None:
        """hook_spec() returns HookSpec with self as observer."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock())
        spec = observer.hook_spec()
        assert observer in spec.observers
        observer.cancel()

    def test_cancel_stops_pending_timer(self) -> None:
        """cancel() stops pending debounce timer -- no flush after cancel."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=0.2)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            observer.on_mutation(_make_write_file_event("/workspace/file.txt"))
            observer.cancel()

            # Wait past what would have been the debounce window
            time.sleep(0.35)

            # Flush should NOT have been called (timer cancelled)
            assert observer._total_flushed == 0

    def test_backward_compat_alias(self) -> None:
        """PipedRecordStoreWriteObserver alias still works."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
            RecordStoreWriteObserver,
        )

        assert PipedRecordStoreWriteObserver is RecordStoreWriteObserver


# ======================================================================
# RecordStoreWriteObserver — Post-flush hook tests (Issue #2978)
# ======================================================================


class TestPostFlushHooks:
    """Verify post-flush hooks are called after successful flush."""

    def test_hook_called_after_flush(self) -> None:
        """Post-flush hook receives events after successful commit."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        # Create mock record store with session factory
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.commit = MagicMock()

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock(return_value=mock_session)

        observer = RecordStoreWriteObserver(mock_record_store, debounce_seconds=0.05)

        # Register a hook that captures events
        captured_events = []

        def capture_hook(events):
            captured_events.extend(events)

        observer.register_post_flush_hook(capture_hook)

        # Simulate a flush batch directly
        test_events = [
            {
                "op": "write",
                "path": "/test.csv",
                "is_new": True,
                "zone_id": "z1",
                "agent_id": None,
                "snapshot_hash": None,
                "metadata_snapshot": None,
                "metadata": {"path": "/test.csv"},
            },
        ]

        # Mock _process_events_in_session to be a no-op
        with patch.object(observer, "_process_events_in_session"):
            observer._flush_batch(test_events)

        assert len(captured_events) == 1
        assert captured_events[0]["path"] == "/test.csv"
        observer.cancel()

    def test_hook_failure_does_not_block_flush(self) -> None:
        """A failing hook must not prevent the audit trail from committing."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.commit = MagicMock()

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock(return_value=mock_session)

        observer = RecordStoreWriteObserver(mock_record_store, debounce_seconds=0.05)

        # Register a hook that raises
        def failing_hook(events):
            raise RuntimeError("Hook failure!")

        # Register a second hook to verify it still runs
        second_hook_called = []

        def second_hook(events):
            second_hook_called.append(True)

        observer.register_post_flush_hook(failing_hook)
        observer.register_post_flush_hook(second_hook)

        test_events = [
            {
                "op": "write",
                "path": "/test.csv",
                "is_new": True,
                "zone_id": None,
                "agent_id": None,
                "snapshot_hash": None,
                "metadata_snapshot": None,
                "metadata": {"path": "/test.csv"},
            }
        ]

        with patch.object(observer, "_process_events_in_session"):
            observer._flush_batch(test_events)

        # Flush succeeded despite hook failure
        assert observer._total_flushed == 1
        # Second hook was still called
        assert len(second_hook_called) == 1
        observer.cancel()

    def test_no_hooks_no_error(self) -> None:
        """Flush works fine with no hooks registered."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.commit = MagicMock()

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock(return_value=mock_session)

        observer = RecordStoreWriteObserver(mock_record_store, debounce_seconds=0.05)

        test_events = [{"op": "mkdir", "path": "/dir", "zone_id": None, "agent_id": None}]

        with patch.object(observer, "_process_events_in_session"):
            observer._flush_batch(test_events)

        assert observer._total_flushed == 1
        observer.cancel()

    def test_register_multiple_hooks(self) -> None:
        """Multiple hooks can be registered."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock()

        observer = RecordStoreWriteObserver(mock_record_store)

        observer.register_post_flush_hook(lambda events: None)
        observer.register_post_flush_hook(lambda events: None)
        observer.register_post_flush_hook(lambda events: None)

        assert len(observer._post_flush_hooks) == 3
        observer.cancel()
