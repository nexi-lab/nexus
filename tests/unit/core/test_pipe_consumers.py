"""Integration tests: OBSERVE-phase + DT_PIPE consumer end-to-end (#926, #809, #810).

Verifies that:
- ZoektWriteObserver receives FileEvents via on_mutation (OBSERVE phase)
  and triggers Zoekt reindex after debounce.
- PipedRecordStoreWriteObserver correctly flows events through the
  DT_PIPE kernel IPC path via NexusFS syscalls.

See: factory/zoekt_observer.py, storage/piped_record_store_write_observer.py
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest

# ======================================================================
# MockNexusFS — simulates pipe I/O via asyncio.Queue
# (still needed for PipedRecordStoreWriteObserver tests below)
# ======================================================================


class MockNexusFS:
    """Minimal NexusFS mock that simulates DT_PIPE via asyncio.Queue.

    Provides sys_setattr, sys_write, sys_read, sys_unlink — the only
    methods PipedRecordStoreWriteObserver calls.
    Also exposes the public sync ``pipe_read_nowait`` / ``pipe_write_nowait``
    convenience methods (Tier 2 NexusFS API) so coalescing consumers
    that drain via non-blocking reads keep working.
    """

    def __init__(self) -> None:
        self._pipes: dict[str, asyncio.Queue[bytes]] = {}
        self._closed: set[str] = set()
        self._pipe_manager = None  # No real PipeManager in tests
        self.write_count = 0

    def pipe_read_nowait(self, path: str) -> bytes | None:
        """Non-blocking drain — returns None when empty (matches Rust semantics)."""
        queue = self._pipes.get(path)
        if queue is None or queue.empty():
            return None
        import queue as _queue

        try:
            return queue.get_nowait()
        except _queue.Empty:
            return None

    def pipe_write_nowait(self, path: str, data: bytes) -> None:
        """Non-blocking write — raises if pipe is closed/missing, else enqueues."""
        if path in self._closed or path not in self._pipes:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path)
        self._pipes[path].put_nowait(data)
        self.write_count += 1

    def sys_setattr(self, path: str, **kwargs: object) -> None:  # noqa: ARG002
        """Create a pipe (stdlib Queue)."""
        import queue as _queue

        if path not in self._pipes:
            self._pipes[path] = _queue.Queue()
        self._closed.discard(path)

    def sys_write(self, path: str, data: bytes, **kwargs: object) -> None:  # noqa: ARG002
        """Write data into the pipe queue."""
        if path in self._closed or path not in self._pipes:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path)
        self._pipes[path].put_nowait(data)

    def sys_read(self, path: str, **kwargs: object) -> bytes:  # noqa: ARG002
        """Read data from the pipe queue.

        Blocks briefly (50ms) when empty — simulates bounded blocking read.
        Raises NexusFileNotFoundError when empty after timeout or pipe closed.
        """
        import queue as _queue

        if path in self._closed or path not in self._pipes:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path)
        try:
            return self._pipes[path].get(timeout=0.05)
        except _queue.Empty:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path) from None

    def sys_unlink(self, path: str, **kwargs: object) -> None:  # noqa: ARG002
        """Close the pipe — subsequent reads raise NexusFileNotFoundError."""
        self._closed.add(path)


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
# PipedRecordStoreWriteObserver — end-to-end tests
# ======================================================================


def _noop_process_events(session: object, events: list[dict[str, object]]) -> None:
    """No-op replacement for _process_events_in_session (avoids sqlalchemy dep)."""


def _make_write_event(path: str, *, is_new: bool = True, size: int = 100) -> bytes:
    """Build a JSON-encoded write event (simulates AuditWriteInterceptor output)."""
    event = {
        "op": "write",
        "path": path,
        "is_new": is_new,
        "zone_id": None,
        "agent_id": None,
        "snapshot_hash": None,
        "metadata_snapshot": None,
        "metadata": {
            "path": path,
            "backend_name": "local",
            "physical_path": f"/data{path}",
            "size": size,
        },
    }
    return json.dumps(event).encode()


def _make_delete_event(path: str) -> bytes:
    """Build a JSON-encoded delete event."""
    event = {"op": "delete", "path": path, "zone_id": None, "agent_id": None}
    return json.dumps(event).encode()


def _make_mkdir_event(path: str) -> bytes:
    """Build a JSON-encoded mkdir event."""
    event = {"op": "mkdir", "path": path, "zone_id": None, "agent_id": None}
    return json.dumps(event).encode()


def _make_rmdir_event(path: str) -> bytes:
    """Build a JSON-encoded rmdir event."""
    event = {"op": "rmdir", "path": path, "zone_id": None, "agent_id": None}
    return json.dumps(event).encode()


class TestPipedWriteObserverE2E:
    """Prove DT_PIPE end-to-end: AuditWriteInterceptor -> pipe -> consumer -> flush.

    We patch ``_process_events_in_session`` (the DB flush layer) so these tests
    verify the DT_PIPE IPC path without requiring sqlalchemy/RecordStore.

    Since the observer is now a pure consumer (Issue #1772), events are written
    to the pipe directly via mock_nx.sys_write (simulating AuditWriteInterceptor).
    """

    @pytest.mark.asyncio
    async def test_write_event_flows_through_pipe(self) -> None:
        """Full E2E: sys_write (producer) -> pipe -> consumer -> _flush_batch."""
        from nexus.storage.piped_record_store_write_observer import (
            _AUDIT_PIPE_PATH,
            PipedRecordStoreWriteObserver,
        )

        mock_nx = MockNexusFS()

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(mock_nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                # Simulate AuditWriteInterceptor writing an event
                mock_nx.sys_write(_AUDIT_PIPE_PATH, _make_write_event("/workspace/test.txt"))
                for _ in range(50):
                    if observer._total_flushed >= 1:
                        break
                    await asyncio.sleep(0.05)
                assert observer._total_flushed >= 1
            finally:
                await observer.stop()

    @pytest.mark.asyncio
    async def test_batch_write_flows_through_pipe(self) -> None:
        """Multiple events enqueued via pipe flush in batches."""
        from nexus.storage.piped_record_store_write_observer import (
            _AUDIT_PIPE_PATH,
            PipedRecordStoreWriteObserver,
        )

        mock_nx = MockNexusFS()

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(mock_nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                for i in range(5):
                    mock_nx.sys_write(
                        _AUDIT_PIPE_PATH,
                        _make_write_event(f"/workspace/file{i}.txt", size=i * 10),
                    )
                for _ in range(50):
                    if observer._total_flushed >= 5:
                        break
                    await asyncio.sleep(0.05)
                assert observer._total_flushed >= 5
            finally:
                await observer.stop()

    @pytest.mark.asyncio
    async def test_delete_event_flows_through_pipe(self) -> None:
        """Delete event flows through pipe to consumer."""
        from nexus.storage.piped_record_store_write_observer import (
            _AUDIT_PIPE_PATH,
            PipedRecordStoreWriteObserver,
        )

        mock_nx = MockNexusFS()

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(mock_nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                mock_nx.sys_write(_AUDIT_PIPE_PATH, _make_delete_event("/workspace/deleted.txt"))
                for _ in range(50):
                    if observer._total_flushed >= 1:
                        break
                    await asyncio.sleep(0.05)
                assert observer._total_flushed >= 1
            finally:
                await observer.stop()

    @pytest.mark.asyncio
    async def test_pre_buffer_drains_on_start(self) -> None:
        """Events buffered in _pre_buffer before bind_fs are drained via flush_sync on stop."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        observer = PipedRecordStoreWriteObserver(MagicMock())

        # Buffer events BEFORE bind_fs (CLI mode / pre-startup)
        observer._pre_buffer.append(_make_write_event("/workspace/early.txt"))
        assert len(observer._pre_buffer) == 1

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            # flush_sync drains pre-buffer directly to DB (no pipe needed)
            flushed = observer.flush_sync()
            assert flushed == 1
            assert len(observer._pre_buffer) == 0
            assert observer._total_flushed >= 1

    def test_flush_sync_for_cli_mode(self) -> None:
        """flush_sync() works without asyncio for CLI shutdown path."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_record_store = MagicMock()
        mock_record_store.session_factory.return_value = mock_session

        observer = PipedRecordStoreWriteObserver(mock_record_store)

        # No bind_fs — events go to pre_buffer directly
        observer._pre_buffer.append(_make_write_event("/workspace/cli.txt"))
        observer._pre_buffer.append(_make_delete_event("/workspace/old.txt"))

        assert len(observer._pre_buffer) == 2

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            flushed = observer.flush_sync()

        assert flushed == 2
        assert observer._total_flushed == 2
        assert len(observer._pre_buffer) == 0

    @pytest.mark.asyncio
    async def test_metrics_tracking(self) -> None:
        """Observer metrics reflect actual event flow."""
        from nexus.storage.piped_record_store_write_observer import (
            _AUDIT_PIPE_PATH,
            PipedRecordStoreWriteObserver,
        )

        mock_nx = MockNexusFS()

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(mock_nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                mock_nx.sys_write(_AUDIT_PIPE_PATH, _make_write_event("/workspace/metrics.txt"))
                mock_nx.sys_write(_AUDIT_PIPE_PATH, _make_mkdir_event("/workspace/newdir"))
                mock_nx.sys_write(_AUDIT_PIPE_PATH, _make_rmdir_event("/workspace/olddir"))

                for _ in range(50):
                    if observer._total_flushed >= 3:
                        break
                    await asyncio.sleep(0.05)

                metrics = observer.metrics
                assert metrics["total_flushed"] >= 3
                assert metrics["total_failed"] == 0
                assert metrics["total_dropped"] == 0
            finally:
                await observer.stop()


# ======================================================================
# PipedRecordStoreWriteObserver — Post-flush hook tests (Issue #2978)
# ======================================================================


class TestPostFlushHooks:
    """Verify post-flush hooks are called after successful flush."""

    @pytest.mark.asyncio
    async def test_hook_called_after_flush(self) -> None:
        """Post-flush hook receives events after successful commit."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        # Create mock record store with session factory
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.commit = MagicMock()

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock(return_value=mock_session)

        observer = PipedRecordStoreWriteObserver(mock_record_store)

        # Register a hook that captures events
        captured_events = []

        def capture_hook(events):
            captured_events.extend(events)

        observer.register_post_flush_hook(capture_hook)

        # Simulate a flush batch
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
            await observer._flush_batch(test_events)

        assert len(captured_events) == 1
        assert captured_events[0]["path"] == "/test.csv"

    @pytest.mark.asyncio
    async def test_hook_failure_does_not_block_flush(self) -> None:
        """A failing hook must not prevent the audit trail from committing."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.commit = MagicMock()

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock(return_value=mock_session)

        observer = PipedRecordStoreWriteObserver(mock_record_store)

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
            await observer._flush_batch(test_events)

        # Flush succeeded despite hook failure
        assert observer._total_flushed == 1
        # Second hook was still called
        assert len(second_hook_called) == 1

    @pytest.mark.asyncio
    async def test_no_hooks_no_error(self) -> None:
        """Flush works fine with no hooks registered."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.commit = MagicMock()

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock(return_value=mock_session)

        observer = PipedRecordStoreWriteObserver(mock_record_store)

        test_events = [{"op": "mkdir", "path": "/dir", "zone_id": None, "agent_id": None}]

        with patch.object(observer, "_process_events_in_session"):
            await observer._flush_batch(test_events)

        assert observer._total_flushed == 1

    def test_register_multiple_hooks(self) -> None:
        """Multiple hooks can be registered."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        mock_record_store = MagicMock()
        mock_record_store.session_factory = MagicMock()

        observer = PipedRecordStoreWriteObserver(mock_record_store)

        observer.register_post_flush_hook(lambda events: None)
        observer.register_post_flush_hook(lambda events: None)
        observer.register_post_flush_hook(lambda events: None)

        assert len(observer._post_flush_hooks) == 3
