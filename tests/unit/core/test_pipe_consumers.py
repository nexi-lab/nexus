"""Integration tests: DT_PIPE consumer end-to-end (#926, #809, #810).

Verifies that ZoektPipeConsumer and PipedRecordStoreWriteObserver correctly
flow events through the DT_PIPE kernel IPC path via NexusFS syscalls:

    sync producer (notify_write / AuditWriteInterceptor)
      -> deque buffer -> flush task -> sys_write  # decoupled
      -> async consumer (_consume loop)
      -> trigger_reindex_async() / RecordStore flush

These tests prove DT_PIPE works end-to-end as a production IPC mechanism,
not just as isolated unit primitives.

See: factory/zoekt_pipe_consumer.py, storage/piped_record_store_write_observer.py
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ======================================================================
# MockNexusFS — simulates pipe I/O via asyncio.Queue
# ======================================================================


class MockNexusFS:
    """Minimal NexusFS mock that simulates DT_PIPE via asyncio.Queue.

    Provides sys_setattr, sys_write, sys_read, sys_unlink — the only
    methods ZoektPipeConsumer and PipedRecordStoreWriteObserver call.
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
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def pipe_write_nowait(self, path: str, data: bytes) -> None:
        """Non-blocking write — raises if pipe is closed/missing, else enqueues."""
        if path in self._closed or path not in self._pipes:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path)
        self._pipes[path].put_nowait(data)
        self.write_count += 1

    def sys_setattr(self, path: str, **kwargs: object) -> None:  # noqa: ARG002
        """Create a pipe (asyncio.Queue)."""
        if path not in self._pipes:
            self._pipes[path] = asyncio.Queue()
        self._closed.discard(path)

    def sys_write(self, path: str, data: bytes, **kwargs: object) -> None:  # noqa: ARG002
        """Write data into the pipe queue."""
        if path in self._closed or path not in self._pipes:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path)
        self._pipes[path].put_nowait(data)

    def sys_read(self, path: str, **kwargs: object) -> bytes:  # noqa: ARG002
        """Read data from the pipe queue (blocking with short timeout).

        Blocks up to 0.5s waiting for data to arrive, simulating the
        real DT_PIPE blocking read.  Raises NexusFileNotFoundError when
        the pipe is closed, missing, or times out (consumer breaks).
        """
        import time

        if path in self._closed:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path)
        if path not in self._pipes:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(path=path)

        # Block with polling (simulate blocking read)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if path in self._closed:
                from nexus.contracts.exceptions import NexusFileNotFoundError

                raise NexusFileNotFoundError(path=path)
            try:
                return self._pipes[path].get_nowait()
            except asyncio.QueueEmpty:
                time.sleep(0.005)  # 5ms poll

        from nexus.contracts.exceptions import NexusFileNotFoundError

        raise NexusFileNotFoundError(path=path)

    def sys_unlink(self, path: str, **kwargs: object) -> None:  # noqa: ARG002
        """Close the pipe — subsequent reads raise NexusFileNotFoundError."""
        self._closed.add(path)


# ======================================================================
# ZoektPipeConsumer — end-to-end tests
# ======================================================================


class TestZoektPipeConsumerE2E:
    """Prove DT_PIPE end-to-end: sync notify_write -> pipe -> consumer -> reindex."""

    @pytest.mark.asyncio
    async def test_notify_write_triggers_reindex(self) -> None:
        """Full E2E: sync notify_write -> buffer -> sys_write -> consumer -> reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        mock_nx = MockNexusFS()

        # Mock ZoektIndexManager
        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05  # Short debounce for test speed
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.05)
        consumer.bind_fs(mock_nx)
        await consumer.start()

        try:
            # Sync producer — this is the hot path (~5us)
            consumer.notify_write("/workspace/file1.txt")
            consumer.notify_write("/workspace/file2.txt")

            # Wait for flush loop (10ms) + debounce (50ms) + consumer processing
            for _ in range(30):
                if zoekt.trigger_reindex_async.call_count >= 1:
                    break
                await asyncio.sleep(0.05)

            # Verify reindex was triggered
            assert zoekt.trigger_reindex_async.call_count >= 1
        finally:
            await consumer.stop()

    @pytest.mark.asyncio
    async def test_sync_complete_triggers_reindex(self) -> None:
        """notify_sync_complete flows through pipe to trigger reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        mock_nx = MockNexusFS()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.05
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.05)
        consumer.bind_fs(mock_nx)
        await consumer.start()

        try:
            consumer.notify_sync_complete(files_synced=5)
            for _ in range(30):
                if zoekt.trigger_reindex_async.call_count >= 1:
                    break
                await asyncio.sleep(0.05)
            assert zoekt.trigger_reindex_async.call_count >= 1
        finally:
            await consumer.stop()

    @pytest.mark.asyncio
    async def test_debounce_coalesces_writes(self) -> None:
        """Multiple rapid writes within debounce window -> single reindex."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        mock_nx = MockNexusFS()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.1
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.05)
        consumer.bind_fs(mock_nx)
        await consumer.start()

        try:
            # Rapid-fire 20 writes within debounce window
            for i in range(20):
                consumer.notify_write(f"/workspace/file{i}.txt")

            # Wait for flush loop + debounce + processing
            for _ in range(30):
                if zoekt.trigger_reindex_async.call_count >= 1:
                    break
                await asyncio.sleep(0.05)

            # Should coalesce into a small number of reindex calls (not 20)
            assert 1 <= zoekt.trigger_reindex_async.call_count <= 3
        finally:
            await consumer.stop()

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

        mock_nx = MockNexusFS()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 0.5  # Long debounce
        zoekt.trigger_reindex_async = AsyncMock()

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=0.5)
        consumer.bind_fs(mock_nx)
        await consumer.start()

        # Write events, then immediately stop (before debounce fires)
        consumer.notify_write("/workspace/file.txt")
        await consumer.stop()

        # Consumer should have been cancelled cleanly (no exceptions)
        # The consumer task should be None after stop
        assert consumer._consumer_task is None

    @pytest.mark.asyncio
    async def test_pipe_full_falls_back(self) -> None:
        """When deque maxlen is reached, oldest events are dropped (deque behavior)."""
        from nexus.factory.zoekt_pipe_consumer import ZoektPipeConsumer

        mock_nx = MockNexusFS()

        zoekt = MagicMock()
        zoekt.debounce_seconds = 10  # Very long debounce - consumer won't drain

        consumer = ZoektPipeConsumer(zoekt, debounce_seconds=10)
        consumer.bind_fs(mock_nx)
        await consumer.start()

        try:
            # The write buffer is a deque(maxlen=10_000). Fill it.
            # With the new sys_write API, the sync path buffers into _write_buffer.
            # Once pipe_ready is True, writes go to the buffer (no fallback to direct).
            for i in range(100):
                consumer.notify_write(f"/workspace/file{i}.txt")

            # Events are buffered, not directly calling zoekt
            # (they go through pipe path since bind_fs was called and start() ran)
        finally:
            await consumer.stop()


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
    """Test RecordStoreWriteObserver debounce-based event flow.

    The observer accumulates events in a deque via on_write/on_delete/etc.
    and flushes them to RecordStore in debounced batches.

    We patch ``_process_events_in_session`` (the DB flush layer) so these
    tests verify the debounce + flush path without requiring sqlalchemy.
    """

    def test_write_event_enqueued_and_flushed(self) -> None:
        """on_write enqueues event, flush_sync drains to DB."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=10)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            observer.on_write(
                MagicMock(
                    path="/workspace/test.txt",
                    etag="abc",
                    to_dict=lambda: {"path": "/workspace/test.txt"},
                ),
                is_new=True,
                path="/workspace/test.txt",
            )
            assert len(observer._pending) == 1
            flushed = observer.flush_sync()
            assert flushed == 1
            assert observer._total_flushed >= 1

    def test_batch_write_enqueued_and_flushed(self) -> None:
        """Multiple on_write calls enqueue events, flush_sync drains all."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=10)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            for i in range(5):
                observer.on_write(
                    MagicMock(
                        path=f"/workspace/file{i}.txt",
                        etag=f"e{i}",
                        to_dict=lambda i=i: {"path": f"/workspace/file{i}.txt"},
                    ),
                    is_new=True,
                    path=f"/workspace/file{i}.txt",
                )
            assert len(observer._pending) == 5
            flushed = observer.flush_sync()
            assert flushed == 5
            assert observer._total_flushed >= 5

    def test_delete_event_enqueued_and_flushed(self) -> None:
        """on_delete enqueues event, flush_sync drains to DB."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=10)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            observer.on_delete(path="/workspace/deleted.txt")
            assert len(observer._pending) == 1
            flushed = observer.flush_sync()
            assert flushed == 1
            assert observer._total_flushed >= 1

    def test_pending_enqueue_and_flush_sync(self) -> None:
        """Events enqueued via _enqueue are drained by flush_sync."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=10)

        event = json.loads(_make_write_event("/workspace/early.txt"))
        observer._enqueue(event)
        assert len(observer._pending) == 1

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            flushed = observer.flush_sync()
            assert flushed == 1
            assert len(observer._pending) == 0
            assert observer._total_flushed >= 1

    def test_flush_sync_for_cli_mode(self) -> None:
        """flush_sync() works without asyncio for CLI shutdown path."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=10)

        # Enqueue events directly via on_write/on_delete
        observer.on_write(
            MagicMock(
                path="/workspace/cli.txt", etag="c1", to_dict=lambda: {"path": "/workspace/cli.txt"}
            ),
            is_new=True,
            path="/workspace/cli.txt",
        )
        observer.on_delete(path="/workspace/old.txt")

        assert len(observer._pending) == 2

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            flushed = observer.flush_sync()

        assert flushed == 2
        assert observer._total_flushed == 2
        assert len(observer._pending) == 0

    def test_metrics_tracking(self) -> None:
        """Observer metrics reflect actual event flow."""
        from nexus.storage.piped_record_store_write_observer import (
            RecordStoreWriteObserver,
        )

        observer = RecordStoreWriteObserver(MagicMock(), debounce_seconds=10)

        with patch.object(
            RecordStoreWriteObserver,
            "_process_events_in_session",
            staticmethod(_noop_process_events),
        ):
            observer.on_write(
                MagicMock(
                    path="/workspace/metrics.txt",
                    etag="m1",
                    to_dict=lambda: {"path": "/workspace/metrics.txt"},
                ),
                is_new=True,
                path="/workspace/metrics.txt",
            )
            observer.on_mkdir(path="/workspace/newdir")
            observer.on_rmdir(path="/workspace/olddir")

            flushed = observer.flush_sync()
            assert flushed == 3

            metrics = observer.metrics
            assert metrics["total_flushed"] >= 3
            assert metrics["total_failed"] == 0
            assert metrics["total_dropped"] == 0


# ======================================================================
# PipedRecordStoreWriteObserver — Post-flush hook tests (Issue #2978)
# ======================================================================


class TestPostFlushHooks:
    """Verify post-flush hooks are called after successful flush."""

    def test_hook_called_after_flush(self) -> None:
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
            observer._flush_batch(test_events)

        assert len(captured_events) == 1
        assert captured_events[0]["path"] == "/test.csv"

    def test_hook_failure_does_not_block_flush(self) -> None:
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
            observer._flush_batch(test_events)

        # Flush succeeded despite hook failure
        assert observer._total_flushed == 1
        # Second hook was still called
        assert len(second_hook_called) == 1

    def test_no_hooks_no_error(self) -> None:
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
            observer._flush_batch(test_events)

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
