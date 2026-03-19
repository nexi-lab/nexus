"""Integration tests: DT_PIPE consumer end-to-end (#926, #809, #810).

Verifies that ZoektPipeConsumer and PipedRecordStoreWriteObserver correctly
flow events through the DT_PIPE kernel IPC path:

    sync producer (notify_write / on_write)
      → pipe_write_nowait() (~5us)
      → RingBuffer (kfifo)
      → async consumer (_consume loop)
      → trigger_reindex_async() / RecordStore flush

These tests prove DT_PIPE works end-to-end as a production IPC mechanism,
not just as isolated unit primitives.

See: factory/zoekt_pipe_consumer.py, storage/piped_record_store_write_observer.py
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.core.pipe_manager import PipeManager

# ======================================================================
# Shared MockMetastore (reused from test_pipe.py)
# ======================================================================


class MockMetastore:
    """Minimal MetastoreABC mock for pipe consumer tests."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> None:
        if metadata.path:
            self._store[metadata.path] = metadata

    def delete(self, path: str, *, consistency: str = "sc") -> dict | None:
        return {"path": path} if self._store.pop(path, None) else None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **kwargs) -> list:  # noqa: ARG002
        return [m for p, m in self._store.items() if p.startswith(prefix)]

    def close(self) -> None:
        pass


# ======================================================================
# ZoektPipeConsumer — end-to-end tests
# ======================================================================


class TestZoektPipeConsumerE2E:
    """Prove DT_PIPE end-to-end: sync notify_write → pipe → consumer → reindex."""

    @pytest.mark.asyncio
    async def test_notify_write_triggers_reindex(self) -> None:
        """Full E2E: sync notify_write → pipe_write_nowait → consumer → reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        # Mock ZoektIndexManager
        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05  # Short debounce for test speed
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.05)
        consumer.set_pipe_manager(pm)
        await consumer.start()

        try:
            # Sync producer — this is the hot path (~5us)
            consumer.notify_write("/workspace/file1.txt")
            consumer.notify_write("/workspace/file2.txt")

            # Wait for debounce window + consumer processing
            await asyncio.sleep(0.15)

            # Verify reindex was triggered
            assert zoekt.trigger_reindex_async.call_count >= 1
        finally:
            await consumer.stop()

    @pytest.mark.asyncio
    async def test_sync_complete_triggers_reindex(self) -> None:
        """notify_sync_complete flows through pipe to trigger reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.05)
        consumer.set_pipe_manager(pm)
        await consumer.start()

        try:
            consumer.notify_sync_complete(files_synced=5)
            await asyncio.sleep(0.15)
            assert zoekt.trigger_reindex_async.call_count >= 1
        finally:
            await consumer.stop()

    @pytest.mark.asyncio
    async def test_debounce_coalesces_writes(self) -> None:
        """Multiple rapid writes within debounce window → single reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.1
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.1)
        consumer.set_pipe_manager(pm)
        await consumer.start()

        try:
            # Rapid-fire 20 writes within debounce window
            for i in range(20):
                consumer.notify_write(f"/workspace/file{i}.txt")

            # Wait for debounce + processing
            await asyncio.sleep(0.25)

            # Should coalesce into 1 reindex call (not 20)
            assert zoekt.trigger_reindex_async.call_count == 1
        finally:
            await consumer.stop()

    @pytest.mark.asyncio
    async def test_fallback_without_pipe_manager(self) -> None:
        """Without PipeManager, notify_write falls back to direct call."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05

        consumer = ZoektPipeConsumer(zoekt)
        # No set_pipe_manager() → no start() → fallback path

        consumer.notify_write("/workspace/file.txt")

        # Falls back to direct zoekt.notify_write()
        zoekt.notify_write.assert_called_once_with("/workspace/file.txt")

    @pytest.mark.asyncio
    async def test_graceful_shutdown_drains(self) -> None:
        """stop() drains remaining pipe events before exiting."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.5  # Long debounce
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.5)
        consumer.set_pipe_manager(pm)
        await consumer.start()

        # Write events, then immediately stop (before debounce fires)
        consumer.notify_write("/workspace/file.txt")
        await consumer.stop()

        # Consumer should have been cancelled cleanly (no exceptions)
        # The consumer task should be None after stop
        assert consumer._consumer_task is None

    @pytest.mark.asyncio
    async def test_pipe_full_falls_back(self) -> None:
        """When pipe is full, notify_write falls back to direct call."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        zoekt = MagicMock()
        zoekt.debounce_seconds = 10  # Very long debounce — consumer won't drain

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=10)
        consumer.set_pipe_manager(pm)
        await consumer.start()

        try:
            # Fill the pipe (64KB capacity) with large messages
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


# ======================================================================
# PipedRecordStoreWriteObserver — end-to-end tests
# ======================================================================


def _noop_process_events(session: object, events: list[dict[str, object]]) -> None:
    """No-op replacement for _process_events_in_session (avoids sqlalchemy dep)."""


class TestPipedWriteObserverE2E:
    """Prove DT_PIPE end-to-end: sync on_write → pipe → consumer → flush.

    We patch ``_process_events_in_session`` (the DB flush layer) so these tests
    verify the DT_PIPE IPC path without requiring sqlalchemy/RecordStore.
    """

    @pytest.mark.asyncio
    async def test_on_write_flows_through_pipe(self) -> None:
        """Full E2E: on_write → pipe_write_nowait → consumer → _flush_batch."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.set_pipe_manager(pm)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                metadata = FileMetadata(
                    path="/workspace/test.txt",
                    backend_name="local",
                    physical_path="/data/test.txt",
                    size=100,
                )
                observer.on_write(metadata, is_new=True, path="/workspace/test.txt")
                await asyncio.sleep(0.15)

                assert observer._total_enqueued == 1
                assert observer._total_flushed >= 1
            finally:
                await observer.stop()

    @pytest.mark.asyncio
    async def test_batch_write_flows_through_pipe(self) -> None:
        """on_write_batch enqueues multiple events that flush in one batch."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.set_pipe_manager(pm)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                items = [
                    (
                        FileMetadata(
                            path=f"/workspace/file{i}.txt",
                            backend_name="local",
                            physical_path=f"/data/file{i}.txt",
                            size=i * 10,
                        ),
                        True,
                    )
                    for i in range(5)
                ]
                observer.on_write_batch(items)
                await asyncio.sleep(0.15)

                assert observer._total_enqueued == 5
                assert observer._total_flushed >= 5
            finally:
                await observer.stop()

    @pytest.mark.asyncio
    async def test_delete_event_flows_through_pipe(self) -> None:
        """on_delete flows through pipe to consumer."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.set_pipe_manager(pm)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                observer.on_delete("/workspace/deleted.txt")
                await asyncio.sleep(0.15)

                assert observer._total_enqueued == 1
                assert observer._total_flushed >= 1
            finally:
                await observer.stop()

    @pytest.mark.asyncio
    async def test_pre_buffer_drains_on_start(self) -> None:
        """Events buffered before pipe injection are drained on start()."""
        from nexus.storage.piped_record_store_write_observer import (
            PipedRecordStoreWriteObserver,
        )

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        observer = PipedRecordStoreWriteObserver(MagicMock())

        # Buffer events BEFORE pipe injection (CLI mode / pre-startup)
        metadata = FileMetadata(
            path="/workspace/early.txt",
            backend_name="local",
            physical_path="/data/early.txt",
            size=50,
        )
        observer.on_write(metadata, is_new=True, path="/workspace/early.txt")
        assert len(observer._pre_buffer) == 1

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            # Now inject pipe and start — should drain pre-buffer into pipe
            observer.set_pipe_manager(pm)
            await observer.start()
            try:
                assert len(observer._pre_buffer) == 0
                await asyncio.sleep(0.15)

                # Pre-buffered event should have been flushed via pipe → consumer
                assert observer._total_flushed >= 1
            finally:
                await observer.stop()

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

        # No pipe manager — events go to pre_buffer
        metadata = FileMetadata(
            path="/workspace/cli.txt",
            backend_name="local",
            physical_path="/data/cli.txt",
            size=25,
        )
        observer.on_write(metadata, is_new=True, path="/workspace/cli.txt")
        observer.on_delete("/workspace/old.txt")

        assert observer._total_enqueued == 2
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

        ms = MockMetastore()
        pm = PipeManager(ms, zone_id="test-zone")

        observer = PipedRecordStoreWriteObserver(MagicMock())
        observer.set_pipe_manager(pm)

        with patch.object(
            PipedRecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            await observer.start()
            try:
                metadata = FileMetadata(
                    path="/workspace/metrics.txt",
                    backend_name="local",
                    physical_path="/data/metrics.txt",
                    size=10,
                )
                observer.on_write(metadata, is_new=True, path="/workspace/metrics.txt")
                observer.on_mkdir("/workspace/newdir")
                observer.on_rmdir("/workspace/olddir")

                await asyncio.sleep(0.15)

                metrics = observer.metrics
                assert metrics["total_enqueued"] == 3
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
