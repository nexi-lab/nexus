"""Unit tests for workflow event pipe (DT_PIPE replacing asyncio.Queue, Task #808).

Tests the integration between NexusFSCoreMixin._fire_workflow_event,
_start_workflow_consumer, ensure_workflow_consumer, and PipeManager.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from nexus.core.metadata import FileMetadata
from nexus.core.pipe_manager import PipeManager

# ======================================================================
# Fixtures
# ======================================================================


class MockMetastore:
    """Minimal MetastoreABC mock."""

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


class FakeCoreMixin:
    """Mimics NexusFSCoreMixin attributes for testing."""

    _WORKFLOW_PIPE_PATH = "/nexus/pipes/workflow-events"
    _WORKFLOW_PIPE_CAPACITY = 65_536

    def __init__(self, *, enable_workflows: bool = True) -> None:
        self.enable_workflows = enable_workflows
        self.workflow_engine = AsyncMock()
        self.subscription_manager = None
        self._pipe_manager: PipeManager | None = PipeManager(MockMetastore(), zone_id="test")
        self._workflow_pipe_ready = False
        self._workflow_consumer_task = None

    # Import the real methods from NexusFSCoreMixin
    from nexus.core.nexus_fs_core import NexusFSCoreMixin

    _fire_workflow_event = NexusFSCoreMixin._fire_workflow_event
    _start_workflow_consumer = NexusFSCoreMixin._start_workflow_consumer
    ensure_workflow_consumer = NexusFSCoreMixin.ensure_workflow_consumer


# ======================================================================
# _fire_workflow_event
# ======================================================================


class TestFireWorkflowEvent:
    def test_writes_to_pipe(self) -> None:
        """Event should be serialized and written to PipeManager."""
        obj = FakeCoreMixin()
        obj.ensure_workflow_consumer()

        obj._fire_workflow_event("file_write", {"path": "/foo.txt"}, "file_write:/foo.txt")

        # Verify data in pipe via peek
        data = obj._pipe_manager.pipe_peek(obj._WORKFLOW_PIPE_PATH)
        assert data is not None
        msg = json.loads(data)
        assert msg["type"] == "file_write"
        assert msg["ctx"]["path"] == "/foo.txt"

    def test_drops_on_full(self) -> None:
        """Overflow should log warning, not raise."""
        obj = FakeCoreMixin()
        # Create pipe with enough capacity for one message but not two
        obj._pipe_manager.create(obj._WORKFLOW_PIPE_PATH, capacity=256, owner_id="kernel")
        obj._workflow_pipe_ready = True

        # Fill the pipe with enough data to overflow
        obj._pipe_manager.pipe_write_nowait(obj._WORKFLOW_PIPE_PATH, b"x" * 256)

        # Should not raise — drops on overflow
        obj._fire_workflow_event("file_write", {"path": "/big.txt"}, "file_write:/big.txt")

    def test_fallback_without_pipe_manager(self) -> None:
        """No pipe manager → fire-and-forget fallback."""
        obj = FakeCoreMixin()
        obj._pipe_manager = None
        obj._workflow_pipe_ready = False

        with patch("nexus.core.sync_bridge.fire_and_forget") as mock_ff:
            obj._fire_workflow_event("file_delete", {"path": "/x"}, "file_delete:/x")
            mock_ff.assert_called_once()

    def test_fallback_before_ensure(self) -> None:
        """Pipe manager exists but ensure_workflow_consumer not called yet."""
        obj = FakeCoreMixin()
        # _workflow_pipe_ready is False, so should use fallback

        with patch("nexus.core.sync_bridge.fire_and_forget") as mock_ff:
            obj._fire_workflow_event("file_write", {"path": "/y"}, "file_write:/y")
            mock_ff.assert_called_once()

    def test_noop_when_workflows_disabled(self) -> None:
        """Should do nothing when workflows are disabled."""
        obj = FakeCoreMixin(enable_workflows=False)
        obj.ensure_workflow_consumer()
        obj._fire_workflow_event("file_write", {"path": "/z"}, "file_write:/z")
        # Pipe should be empty (event was not written)
        assert obj._pipe_manager.pipe_peek(obj._WORKFLOW_PIPE_PATH) is None


# ======================================================================
# _start_workflow_consumer
# ======================================================================


class TestWorkflowConsumer:
    @pytest.mark.asyncio
    async def test_consumer_reads_and_fires(self) -> None:
        """Consumer should deserialize messages and call engine.fire_event."""
        obj = FakeCoreMixin()
        obj.ensure_workflow_consumer()

        # Write events
        for i in range(3):
            obj._fire_workflow_event("file_write", {"idx": i}, f"file_write:{i}")

        # Start consumer as concurrent task
        task = asyncio.create_task(obj._start_workflow_consumer())

        # Wait for consumer to drain all messages
        await asyncio.sleep(0.05)

        # Now shut down — close_all closes buffers then removes from registry
        obj._pipe_manager.close_all()
        await asyncio.wait_for(task, timeout=1.0)

        assert obj.workflow_engine.fire_event.call_count == 3
        calls = obj.workflow_engine.fire_event.call_args_list
        assert calls[0].args == ("file_write", {"idx": 0})
        assert calls[1].args == ("file_write", {"idx": 1})
        assert calls[2].args == ("file_write", {"idx": 2})

    @pytest.mark.asyncio
    async def test_consumer_exits_on_close(self) -> None:
        """Consumer should exit cleanly when pipe is closed."""
        obj = FakeCoreMixin()
        obj.ensure_workflow_consumer()

        # Start consumer, then close pipe
        task = asyncio.create_task(obj._start_workflow_consumer())
        await asyncio.sleep(0.01)
        obj._pipe_manager.close_all()

        # Should not hang
        await asyncio.wait_for(task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_consumer_survives_engine_error(self) -> None:
        """Consumer should continue processing after engine error."""
        obj = FakeCoreMixin()
        obj.ensure_workflow_consumer()

        # Make engine fail on first call, succeed on second
        obj.workflow_engine.fire_event.side_effect = [
            RuntimeError("boom"),
            None,
        ]

        obj._fire_workflow_event("file_write", {"x": 1}, "w:1")
        obj._fire_workflow_event("file_write", {"x": 2}, "w:2")

        # Start consumer concurrently
        task = asyncio.create_task(obj._start_workflow_consumer())
        await asyncio.sleep(0.05)
        obj._pipe_manager.close_all()
        await asyncio.wait_for(task, timeout=1.0)

        # Both were attempted
        assert obj.workflow_engine.fire_event.call_count == 2


# ======================================================================
# ensure_workflow_consumer
# ======================================================================


class TestEnsureWorkflowConsumer:
    def test_creates_pipe(self) -> None:
        """Should create pipe at the correct path with correct capacity."""
        obj = FakeCoreMixin()
        obj.ensure_workflow_consumer()

        assert obj._workflow_pipe_ready is True
        pipes = obj._pipe_manager.list_pipes()
        assert obj._WORKFLOW_PIPE_PATH in pipes
        assert pipes[obj._WORKFLOW_PIPE_PATH]["capacity"] == obj._WORKFLOW_PIPE_CAPACITY

    def test_idempotent(self) -> None:
        """Calling twice should not raise."""
        obj = FakeCoreMixin()
        obj.ensure_workflow_consumer()
        obj.ensure_workflow_consumer()  # should not raise

    def test_noop_without_pipe_manager(self) -> None:
        """No pipe manager (CLI mode) → no-op."""
        obj = FakeCoreMixin()
        obj._pipe_manager = None
        obj.ensure_workflow_consumer()
        assert obj._workflow_pipe_ready is False
