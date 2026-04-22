"""Unit tests for WorkflowDispatchService (#625 partial, #808, #1812).

Tests the service directly — fire(),
start()/stop() lifecycle, and Rust kernel IPC pipe integration.
on_mutation() tests deleted: Rust kernel now dispatches observers directly.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.services.lifecycle.workflow_dispatch_service import WorkflowDispatchService

# ======================================================================
# Fixtures
# ======================================================================

_WORKFLOW_PIPE_PATH = "/nexus/pipes/workflow-events"


def _make_mock_nx() -> MagicMock:
    """Create a mock NexusFS exposing VFS pipe API.

    Production code calls ``self._nx.sys_write`` (DT_PIPE routes through
    Rust dcache) / ``self._nx.pipe_close`` and ``self._nx.sys_setattr`` /
    ``self._nx.sys_read`` (sync).
    """
    nx = MagicMock()
    # Sync passthrough methods
    nx.sys_write = MagicMock(return_value={"path": _WORKFLOW_PIPE_PATH, "bytes_written": 0})
    nx.pipe_close = MagicMock()
    # Sync syscalls — NexusFS methods are sync def
    nx.sys_setattr = MagicMock(return_value={"path": _WORKFLOW_PIPE_PATH, "created": True})
    # sys_read is sync — returns bytes. Raising NexusFileNotFoundError signals
    # "pipe closed" to the consumer loop (which catches it gracefully).
    nx.sys_read = MagicMock(side_effect=NexusFileNotFoundError(_WORKFLOW_PIPE_PATH))
    return nx


def _make_service(
    *, enable_workflows: bool = True, nx: MagicMock | None = None
) -> tuple[WorkflowDispatchService, MagicMock | None]:
    mock_nx = nx or _make_mock_nx()
    engine = AsyncMock()
    svc = WorkflowDispatchService(
        nx=mock_nx,
        workflow_engine=engine,
        enable_workflows=enable_workflows,
    )
    return svc, mock_nx


# ======================================================================
# fire()
# ======================================================================


class TestFire:
    @pytest.mark.asyncio
    async def test_writes_to_pipe(self) -> None:
        """Event should be serialized and written to kernel pipe via sys_write."""
        svc, nx = _make_service()
        await svc.start()

        await svc.fire("file_write", {"path": "/foo.txt"}, "file_write:/foo.txt")

        # Verify sys_write was called with serialized data
        nx.sys_write.assert_called_once()
        call_args = nx.sys_write.call_args
        assert call_args[0][0] == _WORKFLOW_PIPE_PATH
        msg = json.loads(call_args[0][1])
        assert msg["type"] == "file_write"
        assert msg["ctx"]["path"] == "/foo.txt"

        await svc.stop()

    @pytest.mark.asyncio
    async def test_drops_on_full(self) -> None:
        """Overflow should log warning, not raise."""
        svc, nx = _make_service()
        await svc.start()

        # Make sys_write raise to simulate full pipe
        nx.sys_write.side_effect = RuntimeError("PipeFull: buffer full")

        # Should not raise
        await svc.fire("file_write", {"path": "/big.txt"}, "file_write:/big.txt")

    @pytest.mark.asyncio
    async def test_fallback_without_nx(self) -> None:
        """No NexusFS -> direct async call fallback."""
        engine = AsyncMock()
        svc = WorkflowDispatchService(
            nx=None,
            workflow_engine=engine,
            enable_workflows=True,
        )

        await svc.fire("file_delete", {"path": "/x"}, "file_delete:/x")
        engine.fire_event.assert_called_once_with("file_delete", {"path": "/x"})

    @pytest.mark.asyncio
    async def test_fallback_before_start(self) -> None:
        """NexusFS exists but start() not called yet."""
        svc, _ = _make_service()

        await svc.fire("file_write", {"path": "/y"}, "file_write:/y")
        svc._workflow_engine.fire_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_noop_when_workflows_disabled(self) -> None:
        """Should do nothing when workflows are disabled."""
        svc, nx = _make_service(enable_workflows=False)
        await svc.start()

        await svc.fire("file_write", {"path": "/z"}, "file_write:/z")
        # sys_write should NOT be called
        nx.sys_write.assert_not_called()

        await svc.stop()


# ======================================================================
# Consumer loop
# ======================================================================


class TestConsumer:
    @pytest.mark.asyncio
    async def test_consumer_reads_and_fires(self) -> None:
        """Consumer should deserialize messages and call engine.fire_event."""
        svc, nx = _make_service()

        # Set up sys_read to return 3 events then raise (pipe closed)
        from nexus.contracts.exceptions import NexusFileNotFoundError

        events = [json.dumps({"type": "file_write", "ctx": {"idx": i}}).encode() for i in range(3)]
        nx.sys_read = MagicMock(side_effect=[*events, NexusFileNotFoundError(_WORKFLOW_PIPE_PATH)])

        await svc.start()

        # Wait for consumer to drain
        await asyncio.sleep(0.05)
        await svc.stop()

        assert svc._workflow_engine.fire_event.call_count == 3

    @pytest.mark.asyncio
    async def test_consumer_exits_on_close(self) -> None:
        """Consumer should exit cleanly when pipe is closed."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        svc, nx = _make_service()
        nx.sys_read = MagicMock(side_effect=NexusFileNotFoundError(_WORKFLOW_PIPE_PATH))

        await svc.start()
        await asyncio.sleep(0.01)
        await svc.stop()  # should not hang

    @pytest.mark.asyncio
    async def test_consumer_survives_engine_error(self) -> None:
        """Consumer should continue processing after engine error."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        svc, nx = _make_service()

        events = [
            json.dumps({"type": "file_write", "ctx": {"x": 1}}).encode(),
            json.dumps({"type": "file_write", "ctx": {"x": 2}}).encode(),
        ]
        nx.sys_read = MagicMock(side_effect=[*events, NexusFileNotFoundError(_WORKFLOW_PIPE_PATH)])

        svc._workflow_engine.fire_event.side_effect = [
            RuntimeError("boom"),
            None,
        ]

        await svc.start()
        await asyncio.sleep(0.05)
        await svc.stop()

        assert svc._workflow_engine.fire_event.call_count == 2


# ======================================================================
# start() / stop() lifecycle
# ======================================================================


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_pipe(self) -> None:
        """start() should create pipe via sys_setattr."""
        svc, nx = _make_service()
        await svc.start()

        assert svc._pipe_ready is True
        nx.sys_setattr.assert_called_once()

        await svc.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        """Calling start() twice should not raise."""
        svc, nx = _make_service()
        await svc.start()
        await svc.start()  # should not raise

        await svc.stop()

    @pytest.mark.asyncio
    async def test_start_noop_without_nx(self) -> None:
        """No NexusFS (CLI mode) -> no-op."""
        engine = AsyncMock()
        svc = WorkflowDispatchService(
            nx=None,
            workflow_engine=engine,
            enable_workflows=True,
        )
        await svc.start()
        assert svc._pipe_ready is False

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_stop_cancels_consumer(self) -> None:
        """stop() should cancel the consumer task."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        svc, nx = _make_service()
        # sys_read raises immediately (pipe closed) — consumer exits promptly.
        nx.sys_read = MagicMock(side_effect=NexusFileNotFoundError(_WORKFLOW_PIPE_PATH))

        await svc.start()

        assert svc._consumer_task is not None

        await svc.stop()  # should not hang

        assert svc._consumer_task is None
        assert svc._pipe_ready is False
