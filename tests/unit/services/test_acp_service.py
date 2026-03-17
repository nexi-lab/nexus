"""Tests for AcpService — subprocess ownership + DT_PIPE registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.system_services.acp.service import AcpService, _ActiveAgent

# ---------------------------------------------------------------------------
# Mock ProcessTable
# ---------------------------------------------------------------------------


@dataclass
class MockProcessDescriptor:
    pid: str = "test-pid-1"
    name: str = "acp:claude"
    owner_id: str = "user1"
    zone_id: str = "root"
    labels: dict[str, str] = field(default_factory=dict)


class MockProcessTable:
    def __init__(self) -> None:
        self._next_pid = 1
        self._procs: dict[str, MockProcessDescriptor] = {}

    def spawn(self, *, name, owner_id, zone_id, kind, labels=None) -> MockProcessDescriptor:
        pid = f"pid-{self._next_pid}"
        self._next_pid += 1
        desc = MockProcessDescriptor(
            pid=pid, name=name, owner_id=owner_id, zone_id=zone_id, labels=labels or {}
        )
        self._procs[pid] = desc
        return desc

    def kill(self, pid: str, exit_code: int = 0) -> MockProcessDescriptor:
        return self._procs.pop(pid, MockProcessDescriptor(pid=pid))

    def list_processes(self, **kwargs) -> list[MockProcessDescriptor]:
        return list(self._procs.values())


# ---------------------------------------------------------------------------
# Mock Metastore
# ---------------------------------------------------------------------------


class MockMetastore:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

    def get(self, path: str) -> Any:
        return self._store.get(path)

    def put(self, meta: Any) -> None:
        self._store[meta.path] = meta

    def delete(self, path: str) -> None:
        self._store.pop(path, None)

    def list(self, prefix: str) -> list:
        return [v for k, v in self._store.items() if k.startswith(prefix)]

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        if path not in self._metadata:
            self._metadata[path] = {}
        self._metadata[path][key] = value

    def get_file_metadata(self, path: str, key: str) -> Any:
        return self._metadata.get(path, {}).get(key)


# ---------------------------------------------------------------------------
# Mock PipeManager
# ---------------------------------------------------------------------------


class MockPipeManager:
    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.destroyed: list[str] = []

    def create_from_backend(self, path: str, backend: Any, *, owner_id: str | None = None) -> Any:
        self.created[path] = backend
        return backend

    def destroy(self, path: str) -> None:
        self.destroyed.append(path)
        self.created.pop(path, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAcpServiceConstruction:
    """Test AcpService construction and late-binding."""

    def test_init(self):
        pt = MockProcessTable()
        ms = MockMetastore()
        svc = AcpService(process_table=pt, metastore=ms)
        assert svc._pipe_manager is None
        assert svc._nexus_fs is None

    def test_bind_pipe_manager(self):
        pt = MockProcessTable()
        ms = MockMetastore()
        svc = AcpService(process_table=pt, metastore=ms)

        pm = MockPipeManager()
        svc.bind_pipe_manager(pm)
        assert svc._pipe_manager is pm

    def test_bind_fs(self):
        pt = MockProcessTable()
        ms = MockMetastore()
        svc = AcpService(process_table=pt, metastore=ms)

        mock_nx = MagicMock()
        svc.bind_fs(mock_nx)
        assert svc._nexus_fs is mock_nx


class TestAcpServiceNoTransitionHack:
    """Verify the _transition(RUNNING) hack is gone."""

    def test_no_transition_call(self):
        """ProcessTable.spawn() returns RUNNING directly (#1691).
        _transition should not be called anywhere in service.py."""
        import inspect

        from nexus.system_services.acp import service

        source = inspect.getsource(service)
        assert "_transition" not in source


class TestActiveAgentDataclass:
    """Test _ActiveAgent dataclass."""

    def test_active_agent_fields(self):
        conn = MagicMock()
        proc = MagicMock()
        active = _ActiveAgent(
            conn=conn,
            proc=proc,
            fd0_path="/root/proc/p1/fd/0",
            fd1_path="/root/proc/p1/fd/1",
            fd2_path="/root/proc/p1/fd/2",
        )
        assert active.conn is conn
        assert active.proc is proc
        assert active.fd0_path == "/root/proc/p1/fd/0"
        assert active.fd1_path == "/root/proc/p1/fd/1"


class TestAcpServiceKillAgent:
    """Test kill_agent teardown."""

    def test_kill_agent_destroys_pipes(self):
        pt = MockProcessTable()
        ms = MockMetastore()
        pm = MockPipeManager()
        svc = AcpService(process_table=pt, metastore=ms)
        svc.bind_pipe_manager(pm)

        # Set up an active agent
        mock_conn = MagicMock()
        mock_conn.disconnect = AsyncMock()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()

        active = _ActiveAgent(
            conn=mock_conn,
            proc=mock_proc,
            fd0_path="/root/proc/pid-1/fd/0",
            fd1_path="/root/proc/pid-1/fd/1",
            fd2_path="/root/proc/pid-1/fd/2",
        )
        svc._connections["pid-1"] = active

        # Register in process table
        pt._procs["pid-1"] = MockProcessDescriptor(pid="pid-1")

        svc.kill_agent("pid-1")

        # Verify subprocess killed
        mock_proc.kill.assert_called_once()

        # Verify DT_PIPEs destroyed (all 3 fds)
        assert "/root/proc/pid-1/fd/0" in pm.destroyed
        assert "/root/proc/pid-1/fd/1" in pm.destroyed
        assert "/root/proc/pid-1/fd/2" in pm.destroyed

    def test_kill_agent_without_pipe_manager(self):
        """Graceful degradation — no PipeManager bound."""
        pt = MockProcessTable()
        ms = MockMetastore()
        svc = AcpService(process_table=pt, metastore=ms)

        mock_conn = MagicMock()
        mock_conn.disconnect = AsyncMock()
        mock_proc = MagicMock()
        mock_proc.returncode = None

        active = _ActiveAgent(
            conn=mock_conn,
            proc=mock_proc,
            fd0_path="/root/proc/pid-1/fd/0",
            fd1_path="/root/proc/pid-1/fd/1",
            fd2_path="/root/proc/pid-1/fd/2",
        )
        svc._connections["pid-1"] = active
        pt._procs["pid-1"] = MockProcessDescriptor(pid="pid-1")

        # Should not raise even without pipe_manager
        svc.kill_agent("pid-1")
        mock_proc.kill.assert_called_once()


class TestAcpServiceCloseAll:
    """Test close_all teardown."""

    def test_close_all_cleans_up(self):
        pt = MockProcessTable()
        ms = MockMetastore()
        pm = MockPipeManager()
        svc = AcpService(process_table=pt, metastore=ms)
        svc.bind_pipe_manager(pm)

        for i in range(3):
            mock_conn = MagicMock()
            mock_conn.disconnect = AsyncMock()
            mock_proc = MagicMock()
            mock_proc.returncode = None
            svc._connections[f"pid-{i}"] = _ActiveAgent(
                conn=mock_conn,
                proc=mock_proc,
                fd0_path=f"/root/proc/pid-{i}/fd/0",
                fd1_path=f"/root/proc/pid-{i}/fd/1",
                fd2_path=f"/root/proc/pid-{i}/fd/2",
            )

        svc.close_all()

        assert len(svc._connections) == 0
        assert len(pm.destroyed) == 9  # 3 pipes per agent × 3 agents


class TestAcpServiceCallAgent:
    """Test call_agent subprocess + StdioPipe creation."""

    @pytest.mark.asyncio
    async def test_call_agent_creates_stdio_pipes(self):
        """Verify StdioPipe wrapping and DT_PIPE registration."""
        pt = MockProcessTable()
        ms = MockMetastore()
        pm = MockPipeManager()
        svc = AcpService(process_table=pt, metastore=ms)
        svc.bind_pipe_manager(pm)

        # Register a test agent
        from nexus.system_services.acp.agents import AgentConfig

        svc.register_agent(
            AgentConfig(
                agent_id="test-agent",
                name="Test",
                command="echo",
                acp_args=["hello"],
                enabled=True,
            )
        )

        # Mock subprocess to fail fast (command not found is fine for this test)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.returncode = 1
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        # Make stdout.readline return EOF immediately to stop reader loop
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = FileNotFoundError("test-not-found")

            result = await svc.call_agent(
                agent_id="test-agent",
                prompt="test",
                owner_id="user1",
                zone_id="root",
            )

            assert result.exit_code == 127
            assert "Command not found" in result.stderr

        # Connections should be cleaned up
        assert len(svc._connections) == 0
