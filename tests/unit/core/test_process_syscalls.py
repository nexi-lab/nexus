"""Unit tests for NexusFS process syscalls (Issue #2761).

Tests sys_proc_spawn, sys_proc_kill, sys_proc_wait, sys_proc_list,
and sys_dispatch — verifying they delegate to ProcessManager/ToolDispatcher
via the sync bridge and fire the correct hooks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.agent_runtime_types import (
    AgentProcess,
    ExitStatus,
    ProcessState,
    ToolResult,
)
from nexus.contracts.exceptions import BackendError
from nexus.contracts.vfs_hooks import ProcessSpawnHookContext, ProcessTerminateHookContext
from nexus.core.kernel_dispatch import KernelDispatch

# ======================================================================
# Helpers
# ======================================================================


def _make_fs(*, process_manager=None, tool_dispatcher=None):
    """Build a minimal NexusFS-like object with the syscall methods wired.

    Instead of booting the full kernel, we patch the private attributes
    that sys_proc_* and sys_dispatch rely on.  This keeps tests fast and
    isolated from unrelated boot logic.
    """
    from nexus.core.nexus_fs import NexusFS

    # Patch NexusFS.__init__ to avoid full boot
    with patch.object(NexusFS, "__init__", lambda self, *a, **kw: None):
        fs = NexusFS.__new__(NexusFS)

    fs._process_manager = process_manager
    fs._tool_dispatcher = tool_dispatcher
    fs._dispatch = KernelDispatch()
    return fs


def _make_agent_process(
    pid: str = "pid-1",
    agent_id: str = "agent-a",
    zone_id: str = "zone-1",
    state: ProcessState = ProcessState.RUNNING,
    parent_pid: str | None = None,
) -> AgentProcess:
    return AgentProcess(
        pid=pid,
        agent_id=agent_id,
        zone_id=zone_id,
        state=state,
        parent_pid=parent_pid,
        started_at=datetime.now(UTC),
    )


def _make_exit_status(pid: str = "pid-1", exit_code: int = 0) -> ExitStatus:
    return ExitStatus(
        pid=pid,
        exit_code=exit_code,
        reason="completed",
        terminated_at=datetime.now(UTC),
    )


def _make_tool_result(name: str = "read_file") -> ToolResult:
    return ToolResult(
        tool_call_id="tc-1",
        name=name,
        output="hello world",
        duration_ms=12.5,
    )


# ======================================================================
# sys_proc_spawn
# ======================================================================


class TestSysProcSpawn:
    """Tests for NexusFS.sys_proc_spawn."""

    def test_returns_pid_and_state(self):
        pm = MagicMock()
        proc = _make_agent_process()
        pm.spawn = AsyncMock(return_value=proc)
        fs = _make_fs(process_manager=pm)

        with patch("nexus.lib.sync_bridge.run_sync", return_value=proc):
            result = fs.sys_proc_spawn("agent-a", "zone-1")

        assert result["pid"] == "pid-1"
        assert result["agent_id"] == "agent-a"
        assert result["zone_id"] == "zone-1"
        assert result["state"] == "running"

    def test_fires_post_spawn_hook(self):
        pm = MagicMock()
        proc = _make_agent_process()
        pm.spawn = AsyncMock(return_value=proc)
        fs = _make_fs(process_manager=pm)

        hook = MagicMock()
        hook.name = "test_hook"
        hook.on_pre_proc_spawn = MagicMock()
        hook.on_post_proc_spawn = MagicMock()
        fs._dispatch.register_intercept_process(hook)

        with patch("nexus.lib.sync_bridge.run_sync", return_value=proc):
            fs.sys_proc_spawn("agent-a", "zone-1")

        hook.on_post_proc_spawn.assert_called_once()
        ctx = hook.on_post_proc_spawn.call_args[0][0]
        assert isinstance(ctx, ProcessSpawnHookContext)
        assert ctx.pid == "pid-1"
        assert ctx.agent_id == "agent-a"

    def test_raises_when_no_process_manager(self):
        fs = _make_fs(process_manager=None)

        with pytest.raises(BackendError, match="ProcessManager not available"):
            fs.sys_proc_spawn("agent-a", "zone-1")

    def test_passes_parent_pid_and_metadata(self):
        pm = MagicMock()
        proc = _make_agent_process(parent_pid="parent-0")
        pm.spawn = AsyncMock(return_value=proc)
        fs = _make_fs(process_manager=pm)

        with patch("nexus.lib.sync_bridge.run_sync", return_value=proc):
            result = fs.sys_proc_spawn(
                "agent-a",
                "zone-1",
                parent_pid="parent-0",
                metadata={"key": "val"},
            )

        assert result["parent_pid"] == "parent-0"


# ======================================================================
# sys_proc_kill
# ======================================================================


class TestSysProcKill:
    """Tests for NexusFS.sys_proc_kill."""

    def test_returns_terminated_true(self):
        pm = MagicMock()
        proc = _make_agent_process()
        pm.get_process = AsyncMock(return_value=proc)
        pm.terminate = AsyncMock(return_value=True)
        fs = _make_fs(process_manager=pm)

        with patch("nexus.lib.sync_bridge.run_sync", side_effect=[proc, True]):
            result = fs.sys_proc_kill("pid-1")

        assert result["pid"] == "pid-1"
        assert result["terminated"] is True

    def test_fires_terminate_hook(self):
        pm = MagicMock()
        proc = _make_agent_process()
        pm.get_process = AsyncMock(return_value=proc)
        pm.terminate = AsyncMock(return_value=True)
        fs = _make_fs(process_manager=pm)

        hook = MagicMock()
        hook.name = "test_hook"
        hook.on_post_proc_terminate = MagicMock()
        fs._dispatch.register_intercept_process(hook)

        with patch("nexus.lib.sync_bridge.run_sync", side_effect=[proc, True]):
            fs.sys_proc_kill("pid-1", reason="user-cancel")

        hook.on_post_proc_terminate.assert_called_once()
        ctx = hook.on_post_proc_terminate.call_args[0][0]
        assert isinstance(ctx, ProcessTerminateHookContext)
        assert ctx.reason == "user-cancel"

    def test_raises_when_no_process_manager(self):
        fs = _make_fs(process_manager=None)

        with pytest.raises(BackendError, match="ProcessManager not available"):
            fs.sys_proc_kill("pid-1")


# ======================================================================
# sys_proc_wait
# ======================================================================


class TestSysProcWait:
    """Tests for NexusFS.sys_proc_wait."""

    def test_returns_exit_status(self):
        pm = MagicMock()
        status = _make_exit_status()
        pm.wait = AsyncMock(return_value=status)
        fs = _make_fs(process_manager=pm)

        with patch("nexus.lib.sync_bridge.run_sync", return_value=status):
            result = fs.sys_proc_wait("pid-1")

        assert result["pid"] == "pid-1"
        assert result["exit_code"] == 0
        assert result["reason"] == "completed"
        assert "terminated_at" in result

    def test_raises_when_no_process_manager(self):
        fs = _make_fs(process_manager=None)

        with pytest.raises(BackendError, match="ProcessManager not available"):
            fs.sys_proc_wait("pid-1")


# ======================================================================
# sys_proc_list
# ======================================================================


class TestSysProcList:
    """Tests for NexusFS.sys_proc_list."""

    def test_returns_process_list(self):
        pm = MagicMock()
        procs = [
            _make_agent_process(pid="p1", zone_id="zone-1"),
            _make_agent_process(pid="p2", zone_id="zone-1"),
        ]
        pm.list_processes = AsyncMock(return_value=procs)
        fs = _make_fs(process_manager=pm)

        with patch("nexus.lib.sync_bridge.run_sync", return_value=procs):
            result = fs.sys_proc_list(zone_id="zone-1")

        assert len(result) == 2
        assert result[0]["pid"] == "p1"
        assert result[1]["pid"] == "p2"

    def test_filters_by_zone(self):
        pm = MagicMock()
        procs = [_make_agent_process(pid="p1", zone_id="zone-2")]
        pm.list_processes = AsyncMock(return_value=procs)
        fs = _make_fs(process_manager=pm)

        with patch("nexus.lib.sync_bridge.run_sync", return_value=procs):
            result = fs.sys_proc_list(zone_id="zone-2")

        assert len(result) == 1
        assert result[0]["zone_id"] == "zone-2"

    def test_raises_when_no_process_manager(self):
        fs = _make_fs(process_manager=None)

        with pytest.raises(BackendError, match="ProcessManager not available"):
            fs.sys_proc_list()


# ======================================================================
# sys_dispatch
# ======================================================================


class TestSysDispatch:
    """Tests for NexusFS.sys_dispatch."""

    def test_routes_tool_call(self):
        td = MagicMock()
        tool_result = _make_tool_result()
        td.dispatch = AsyncMock(return_value=tool_result)
        fs = _make_fs(tool_dispatcher=td)

        with patch("nexus.lib.sync_bridge.run_sync", return_value=tool_result):
            result = fs.sys_dispatch(
                "read_file",
                {"path": "/test"},
                agent_id="agent-a",
                zone_id="zone-1",
            )

        assert result["name"] == "read_file"
        assert result["output"] == "hello world"
        assert result["success"] is True
        assert result["error"] is None

    def test_raises_when_no_tool_dispatcher(self):
        fs = _make_fs(tool_dispatcher=None)

        with pytest.raises(BackendError, match="ToolDispatcher not available"):
            fs.sys_dispatch(
                "read_file",
                {},
                agent_id="agent-a",
                zone_id="zone-1",
            )
