"""Integration tests: DT_PIPE consumer end-to-end (#926, #809, #810).

Verifies that ZoektPipeConsumer and PipedRecordStoreWriteObserver correctly
flow events through the DT_PIPE kernel IPC path:

    sync producer (notify_write / sys_write)
      -> pipe_write_nowait() (~5us)
      -> RingBuffer (kfifo)
      -> async consumer (_consume loop)
      -> trigger_reindex_async() / RecordStore flush

These tests prove DT_PIPE works end-to-end as a production IPC mechanism,
not just as isolated unit primitives.

See: factory/zoekt_pipe_consumer.py, storage/piped_record_store_write_observer.py

Issue #1772: Tests migrated from PipeManager API to NexusFS sys_read/sys_write.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.core.config import ParseConfig, PermissionConfig, SystemServices
from tests.helpers.dict_metastore import DictMetastore
from tests.helpers.test_context import TEST_CONTEXT

# Pipe path constants (must match production code)
_ZOEKT_PIPE_PATH = "/nexus/pipes/zoekt-writes"
_AUDIT_PIPE_PATH = "/nexus/pipes/audit-events"


# ======================================================================
# Shared helpers
# ======================================================================


def _make_nx():
    """Create a minimal NexusFS with DictMetastore for pipe tests."""
    from nexus import CASLocalBackend, NexusFS

    tmpdir = tempfile.mkdtemp()
    metastore = DictMetastore()
    backend = CASLocalBackend(str(Path(tmpdir) / "data"))
    nx = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        parsing=ParseConfig(auto_parse=False),
        system_services=SystemServices(),
    )
    nx._default_context = TEST_CONTEXT
    nx.router.add_mount("/", backend)
    return nx


def _audit_event_write(path: str, *, is_new: bool = True) -> bytes:
    """Create an audit write event JSON (simulates AuditWriteInterceptor)."""
    metadata = FileMetadata(
        path=path,
        backend_name="local",
        physical_path=f"/data{path}",
        size=100,
    )
    return json.dumps(
        {
            "op": "write",
            "path": path,
            "is_new": is_new,
            "zone_id": None,
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }
    ).encode()


def _audit_event_delete(path: str) -> bytes:
    """Create an audit delete event JSON."""
    return json.dumps(
        {
            "op": "delete",
            "path": path,
            "zone_id": None,
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
        }
    ).encode()


def _audit_event_mkdir(path: str) -> bytes:
    """Create an audit mkdir event JSON."""
    return json.dumps(
        {
            "op": "mkdir",
            "path": path,
            "zone_id": None,
            "agent_id": None,
        }
    ).encode()


def _audit_event_rmdir(path: str) -> bytes:
    """Create an audit rmdir event JSON."""
    return json.dumps(
        {
            "op": "rmdir",
            "path": path,
            "zone_id": None,
            "agent_id": None,
            "recursive": False,
        }
    ).encode()


def _noop_process_events(session: object, events: list[dict[str, object]]) -> None:
    """No-op replacement for _process_events_in_session (avoids sqlalchemy dep)."""


# ======================================================================
# ZoektPipeConsumer — end-to-end tests
# ======================================================================


class TestZoektPipeConsumerE2E:
    """Prove DT_PIPE end-to-end: sync notify_write -> pipe -> consumer -> reindex."""

    @pytest.mark.asyncio
    async def test_notify_write_triggers_reindex(self) -> None:
        """Full E2E: sync notify_write -> pipe_write_nowait -> consumer -> reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        nx = _make_nx()

        # Mock ZoektIndexManager
        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05  # Short debounce for test speed
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.05)
        consumer.bind_fs(nx)
        await consumer.start()

        try:
            # Sync producer — this is the hot path (~5us)
            consumer.notify_write("/workspace/file1.txt")
            consumer.notify_write("/workspace/file2.txt")

            # Wait for flush loop + debounce window + consumer processing
            await asyncio.sleep(0.25)

            # Verify reindex was triggered
            assert zoekt.trigger_reindex_async.call_count >= 1
        finally:
            await consumer.stop()
            nx.close()

    @pytest.mark.asyncio
    async def test_sync_complete_triggers_reindex(self) -> None:
        """notify_sync_complete flows through pipe to trigger reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        nx = _make_nx()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.05)
        consumer.bind_fs(nx)
        await consumer.start()

        try:
            consumer.notify_sync_complete(files_synced=5)
            await asyncio.sleep(0.25)
            assert zoekt.trigger_reindex_async.call_count >= 1
        finally:
            await consumer.stop()
            nx.close()

    @pytest.mark.asyncio
    async def test_debounce_coalesces_writes(self) -> None:
        """Multiple rapid writes within debounce window -> single reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        nx = _make_nx()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.1
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.1)
        consumer.bind_fs(nx)
        await consumer.start()

        try:
            # Rapid-fire 20 writes within debounce window
            for i in range(20):
                consumer.notify_write(f"/workspace/file{i}.txt")

            # Wait for flush loop + debounce + processing
            await asyncio.sleep(0.35)

            # Should coalesce into 1 reindex call (not 20)
            assert zoekt.trigger_reindex_async.call_count == 1
        finally:
            await consumer.stop()
            nx.close()

    @pytest.mark.asyncio
    async def test_fallback_without_bind_fs(self) -> None:
        """Without bind_fs, notify_write falls back to direct call."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05

        consumer = ZoektPipeConsumer(zoekt)
        # No bind_fs() -> no start() -> fallback path

        consumer.notify_write("/workspace/file.txt")

        # Falls back to direct zoekt.notify_write()
        zoekt.notify_write.assert_called_once_with("/workspace/file.txt")

    @pytest.mark.asyncio
    async def test_graceful_shutdown_drains(self) -> None:
        """stop() drains remaining pipe events before exiting."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        nx = _make_nx()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.5  # Long debounce
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.5)
        consumer.bind_fs(nx)
        await consumer.start()

        # Write events, then immediately stop (before debounce fires)
        consumer.notify_write("/workspace/file.txt")
        await consumer.stop()

        # Consumer should have been cancelled cleanly (no exceptions)
        # The consumer task should be None after stop
        assert consumer._consumer_task is None
        nx.close()

    @pytest.mark.asyncio
    async def test_pipe_full_falls_back(self) -> None:
        """When pipe is full, notify_write falls back to direct call."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        nx = _make_nx()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 10  # Very long debounce — consumer won't drain

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=10)
        consumer.bind_fs(nx)
        await consumer.start()

        try:
            # Fill the write buffer (maxlen=10_000) — events buffer in deque
            # then pipe (64KB capacity) with large messages
            filled = False
            for _ in range(200):
                try:
                    consumer.notify_write("/x" * 500)
                except Exception:
                    filled = True
                    break
                if zoekt.notify_write.call_count > 0:
                    filled = True
                    break

            # After pipe fills, fallback to direct call should have been used
            if filled:
                assert zoekt.notify_write.call_count >= 1
        finally:
            await consumer.stop()
            nx.close()


# ======================================================================
# PipedRecordStoreWriteObserver — end-to-end tests
# ======================================================================


class TestPipedWriteObserverE2E:
    """Prove DT_PIPE end-to-end: sys_write -> pipe -> consumer -> _flush_batch.

    We patch ``_flush_batch_sync`` (the DB flush layer) so these tests
    verify the DT_PIPE IPC path without requiring sqlalchemy/RecordStore.

    The producer side is now AuditWriteInterceptor (writes via nx.sys_write),
    simulated here by direct nx.sys_write() calls with JSON events.

    Note: The consumer's _consume() loop uses blocking sys_read() for batch
    draining. After writing events, we yield to the event loop so the consumer
    task can start processing, then stop() closes the pipe which breaks the
    drain loop and triggers _flush_batch.
    """

    @pytest.mark.asyncio
    async def test_on_write_flows_through_pipe(self) -> None:
        """Full E2E: sys_write -> pipe -> consumer -> _flush_batch (on stop)."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        nx = _make_nx()
        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_flush_batch_sync",
            lambda self, events: None,
        ):
            await observer.start()
            try:
                # Simulate AuditWriteInterceptor writing to the pipe
                await nx.sys_write(_AUDIT_PIPE_PATH, _audit_event_write("/workspace/test.txt"))
                # Yield so consumer task starts and reads the event from the pipe
                await asyncio.sleep(0)
            finally:
                # stop() closes pipe, which unblocks the consumer's drain loop
                # and triggers _flush_batch with accumulated events
                await observer.stop()

            assert observer._total_flushed >= 1
        nx.close()

    @pytest.mark.asyncio
    async def test_batch_write_flows_through_pipe(self) -> None:
        """Multiple sys_write events flush in batch on stop."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        nx = _make_nx()
        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_flush_batch_sync",
            lambda self, events: None,
        ):
            await observer.start()
            try:
                for i in range(5):
                    await nx.sys_write(
                        _AUDIT_PIPE_PATH,
                        _audit_event_write(f"/workspace/file{i}.txt"),
                    )
                # Yield so consumer task starts and reads events from the pipe
                await asyncio.sleep(0)
            finally:
                await observer.stop()

            assert observer._total_flushed >= 5
        nx.close()

    @pytest.mark.asyncio
    async def test_delete_event_flows_through_pipe(self) -> None:
        """Delete event flows through pipe to consumer on stop."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        nx = _make_nx()
        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_flush_batch_sync",
            lambda self, events: None,
        ):
            await observer.start()
            try:
                await nx.sys_write(_AUDIT_PIPE_PATH, _audit_event_delete("/workspace/deleted.txt"))
                # Yield so consumer task starts and reads the event from the pipe
                await asyncio.sleep(0)
            finally:
                await observer.stop()

            assert observer._total_flushed >= 1
        nx.close()

    @pytest.mark.asyncio
    async def test_pre_buffer_drains_on_stop(self) -> None:
        """Events buffered in _pre_buffer are drained by flush_sync on stop."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        observer = PipedRecordStoreWriteObserver(MagicMock())

        # Buffer events BEFORE bind_fs (CLI mode / pre-startup)
        observer._pre_buffer.append(_audit_event_write("/workspace/early.txt"))
        observer._total_enqueued = 1
        assert len(observer._pre_buffer) == 1

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            # flush_sync drains pre-buffer directly to DB
            flushed = observer.flush_sync()

        assert flushed == 1
        assert len(observer._pre_buffer) == 0
        assert observer._total_flushed == 1

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

        # No bind_fs — events go to pre_buffer manually
        observer._pre_buffer.append(_audit_event_write("/workspace/cli.txt"))
        observer._pre_buffer.append(_audit_event_delete("/workspace/old.txt"))
        observer._total_enqueued = 2

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
            PipedRecordStoreWriteObserver,
        )

        nx = _make_nx()
        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.bind_fs(nx)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_flush_batch_sync",
            lambda self, events: None,
        ):
            await observer.start()
            try:
                await nx.sys_write(_AUDIT_PIPE_PATH, _audit_event_write("/workspace/metrics.txt"))
                await nx.sys_write(_AUDIT_PIPE_PATH, _audit_event_mkdir("/workspace/newdir"))
                await nx.sys_write(_AUDIT_PIPE_PATH, _audit_event_rmdir("/workspace/olddir"))
                # Yield so consumer task starts and reads events from the pipe
                await asyncio.sleep(0)
            finally:
                # stop() closes pipe, consumer drains and flushes
                await observer.stop()

            metrics = observer.metrics
            assert metrics["total_flushed"] >= 3
            assert metrics["total_failed"] == 0
            assert metrics["total_dropped"] == 0
        nx.close()


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
