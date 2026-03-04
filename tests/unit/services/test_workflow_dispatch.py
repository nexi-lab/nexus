"""Unit tests for WorkflowDispatchService (#625 partial, #808).

Tests the service directly (not via FakeCoreMixin) — fire(), on_mutation(),
start()/stop() lifecycle, and PipeManager integration.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.core.file_events import FileEvent, FileEventType
from nexus.system_services.lifecycle.workflow_dispatch_service import WorkflowDispatchService
from nexus.system_services.pipe_manager import PipeManager

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


def _make_service(
    *, enable_workflows: bool = True, pipe_manager: PipeManager | None = None
) -> tuple[WorkflowDispatchService, PipeManager | None]:
    pm = pipe_manager or PipeManager(MockMetastore(), zone_id="test")
    engine = AsyncMock()
    svc = WorkflowDispatchService(
        pipe_manager=pm,
        workflow_engine=engine,
        enable_workflows=enable_workflows,
    )
    return svc, pm


# ======================================================================
# fire()
# ======================================================================


class TestFire:
    @pytest.mark.asyncio
    async def test_writes_to_pipe(self) -> None:
        """Event should be serialized and written to PipeManager."""
        svc, pm = _make_service()
        # Must start to create pipe
        await svc.start()

        svc.fire("file_write", {"path": "/foo.txt"}, "file_write:/foo.txt")

        data = pm.pipe_peek("/nexus/pipes/workflow-events")
        assert data is not None
        msg = json.loads(data)
        assert msg["type"] == "file_write"
        assert msg["ctx"]["path"] == "/foo.txt"

        await svc.stop()

    def test_drops_on_full(self) -> None:
        """Overflow should log warning, not raise."""
        svc, pm = _make_service()
        # Create pipe with small capacity
        pm.create("/nexus/pipes/workflow-events", capacity=256, owner_id="kernel")
        svc._pipe_ready = True

        # Fill the pipe
        pm.pipe_write_nowait("/nexus/pipes/workflow-events", b"x" * 256)

        # Should not raise
        svc.fire("file_write", {"path": "/big.txt"}, "file_write:/big.txt")

    def test_fallback_without_pipe_manager(self) -> None:
        """No pipe manager -> fire-and-forget fallback."""
        engine = AsyncMock()
        svc = WorkflowDispatchService(
            pipe_manager=None,
            workflow_engine=engine,
            enable_workflows=True,
        )

        with patch("nexus.lib.sync_bridge.fire_and_forget") as mock_ff:
            svc.fire("file_delete", {"path": "/x"}, "file_delete:/x")
            mock_ff.assert_called_once()

    def test_fallback_before_start(self) -> None:
        """Pipe manager exists but start() not called yet."""
        svc, _ = _make_service()

        with patch("nexus.lib.sync_bridge.fire_and_forget") as mock_ff:
            svc.fire("file_write", {"path": "/y"}, "file_write:/y")
            mock_ff.assert_called_once()

    @pytest.mark.asyncio
    async def test_noop_when_workflows_disabled(self) -> None:
        """Should do nothing when workflows are disabled."""
        svc, pm = _make_service(enable_workflows=False)
        await svc.start()

        svc.fire("file_write", {"path": "/z"}, "file_write:/z")
        # Pipe should be empty
        assert pm.pipe_peek("/nexus/pipes/workflow-events") is None

        await svc.stop()


# ======================================================================
# on_mutation() — VFSObserver
# ======================================================================


class TestOnMutation:
    @pytest.mark.asyncio
    async def test_write_event(self) -> None:
        """on_mutation(WRITE) should call fire() with correct trigger type."""
        svc, pm = _make_service()
        await svc.start()

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="root",
            agent_id="agent-1",
            user_id="user-1",
            timestamp="2026-02-19T00:00:00",
            etag="abc123",
            size=1024,
            version=3,
            is_new=True,
        )
        svc.on_mutation(event)

        data = pm.pipe_peek("/nexus/pipes/workflow-events")
        assert data is not None
        msg = json.loads(data)
        assert msg["type"] == "file_write"
        assert msg["ctx"]["file_path"] == "/test/file.txt"
        assert msg["ctx"]["created"] is True

        await svc.stop()

    @pytest.mark.asyncio
    async def test_delete_event(self) -> None:
        """on_mutation(DELETE) should produce file_delete trigger."""
        svc, pm = _make_service()
        await svc.start()

        event = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/test/gone.txt",
            zone_id="root",
            version=43,
        )
        svc.on_mutation(event)

        data = pm.pipe_peek("/nexus/pipes/workflow-events")
        msg = json.loads(data)
        assert msg["type"] == "file_delete"
        assert msg["ctx"]["file_path"] == "/test/gone.txt"

        await svc.stop()

    @pytest.mark.asyncio
    async def test_rename_event(self) -> None:
        """on_mutation(RENAME) should produce file_rename trigger with old/new paths."""
        svc, pm = _make_service()
        await svc.start()

        event = FileEvent(
            type=FileEventType.FILE_RENAME,
            path="/old/path.txt",
            zone_id="root",
            version=44,
            new_path="/new/path.txt",
        )
        svc.on_mutation(event)

        data = pm.pipe_peek("/nexus/pipes/workflow-events")
        msg = json.loads(data)
        assert msg["type"] == "file_rename"
        assert msg["ctx"]["old_path"] == "/old/path.txt"
        assert msg["ctx"]["new_path"] == "/new/path.txt"

        await svc.stop()


# ======================================================================
# Consumer loop
# ======================================================================


class TestConsumer:
    @pytest.mark.asyncio
    async def test_consumer_reads_and_fires(self) -> None:
        """Consumer should deserialize messages and call engine.fire_event."""
        svc, pm = _make_service()
        await svc.start()

        # Write events directly
        for i in range(3):
            svc.fire("file_write", {"idx": i}, f"file_write:{i}")

        # Wait for consumer to drain
        await asyncio.sleep(0.05)

        # Shut down
        pm.close_all()
        await svc.stop()

        assert svc._workflow_engine.fire_event.call_count == 3

    @pytest.mark.asyncio
    async def test_consumer_exits_on_close(self) -> None:
        """Consumer should exit cleanly when pipe is closed."""
        svc, pm = _make_service()
        await svc.start()

        await asyncio.sleep(0.01)
        pm.close_all()
        await svc.stop()  # should not hang

    @pytest.mark.asyncio
    async def test_consumer_survives_engine_error(self) -> None:
        """Consumer should continue processing after engine error."""
        svc, pm = _make_service()
        await svc.start()

        svc._workflow_engine.fire_event.side_effect = [
            RuntimeError("boom"),
            None,
        ]

        svc.fire("file_write", {"x": 1}, "w:1")
        svc.fire("file_write", {"x": 2}, "w:2")

        await asyncio.sleep(0.05)
        pm.close_all()
        await svc.stop()

        assert svc._workflow_engine.fire_event.call_count == 2


# ======================================================================
# start() / stop() lifecycle
# ======================================================================


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_pipe(self) -> None:
        """start() should create pipe at the correct path."""
        svc, pm = _make_service()
        await svc.start()

        assert svc._pipe_ready is True
        pipes = pm.list_pipes()
        assert "/nexus/pipes/workflow-events" in pipes

        await svc.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Calling start() twice should not raise."""
        svc, pm = _make_service()
        await svc.start()
        await svc.start()  # should not raise

        pm.close_all()
        await svc.stop()

    @pytest.mark.asyncio
    async def test_start_noop_without_pipe_manager(self) -> None:
        """No pipe manager (CLI mode) -> no-op."""
        engine = AsyncMock()
        svc = WorkflowDispatchService(
            pipe_manager=None,
            workflow_engine=engine,
            enable_workflows=True,
        )
        await svc.start()
        assert svc._pipe_ready is False

    @pytest.mark.asyncio
    async def test_stop_cancels_consumer(self) -> None:
        """stop() should cancel the consumer task."""
        svc, pm = _make_service()
        await svc.start()

        assert svc._consumer_task is not None
        assert not svc._consumer_task.done()

        pm.close_all()
        await svc.stop()

        assert svc._consumer_task is None
        assert svc._pipe_ready is False
