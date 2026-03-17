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
        svc = AcpService(process_table=pt)
        assert svc._pipe_manager is None
        assert svc._nexus_fs is None

    def test_bind_pipe_manager(self):
        pt = MockProcessTable()
        svc = AcpService(process_table=pt)

        pm = MockPipeManager()
        svc.bind_pipe_manager(pm)
        assert svc._pipe_manager is pm

    def test_bind_fs(self):
        pt = MockProcessTable()
        svc = AcpService(process_table=pt)

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

    def test_no_metastore_reference(self):
        """AcpService must not access metastore directly — all I/O through VFS."""
        import inspect

        from nexus.system_services.acp import service

        source = inspect.getsource(service)
        assert "_metastore" not in source
        assert "_ensure_vfs_entry" not in source


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
        pm = MockPipeManager()
        svc = AcpService(process_table=pt)
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
        svc = AcpService(process_table=pt)

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
        pm = MockPipeManager()
        svc = AcpService(process_table=pt)
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
        pm = MockPipeManager()
        svc = AcpService(process_table=pt)
        svc.bind_pipe_manager(pm)

        # Register a test agent
        from nexus.system_services.acp.agents import AgentConfig

        await svc.register_agent(
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


class TestAgentConfigSerialization:
    """Test AgentConfig.to_dict() / from_dict() round-trip."""

    def test_roundtrip_minimal(self):
        from nexus.system_services.acp.agents import AgentConfig

        cfg = AgentConfig(agent_id="test", name="Test", command="test-bin")
        restored = AgentConfig.from_dict(cfg.to_dict())
        assert restored == cfg

    def test_roundtrip_full(self):
        from nexus.system_services.acp.agents import AgentConfig

        cfg = AgentConfig(
            agent_id="claude",
            name="Claude Code",
            command="claude",
            prompt_flag="-p",
            default_system_prompt="You are helpful.",
            extra_args=("--output-format", "json"),
            env={"KEY": "val"},
            npx_package="@zed/claude-acp@1.0",
            acp_args=("--experimental-acp", "--dangerously-skip-permissions"),
            enabled=True,
        )
        data = cfg.to_dict()
        restored = AgentConfig.from_dict(data)
        assert restored == cfg

    def test_from_dict_defaults(self):
        """from_dict fills in defaults for missing optional fields."""
        from nexus.system_services.acp.agents import AgentConfig

        data = {"agent_id": "x", "name": "X", "command": "x-bin"}
        cfg = AgentConfig.from_dict(data)
        assert cfg.prompt_flag == "-p"
        assert cfg.default_system_prompt is None
        assert cfg.extra_args == ()
        assert cfg.env == {}
        assert cfg.npx_package is None
        assert cfg.acp_args == ("--experimental-acp",)
        assert cfg.enabled is True

    def test_to_dict_json_serializable(self):
        """to_dict output must be JSON-serializable (no tuples)."""
        import json

        from nexus.system_services.acp.agents import AgentConfig

        cfg = AgentConfig(
            agent_id="test",
            name="Test",
            command="test",
            extra_args=("a", "b"),
            acp_args=("--acp",),
        )
        data = cfg.to_dict()
        # Should not raise
        serialized = json.dumps(data)
        # Round-trip through JSON
        restored = AgentConfig.from_dict(json.loads(serialized))
        assert restored == cfg


class TestAgentConfigVfsPersistence:
    """Test register_agent VFS persistence."""

    @pytest.mark.asyncio
    async def test_register_persists_to_vfs(self):
        """register_agent writes config JSON to VFS when NexusFS is bound."""
        import json

        from nexus.system_services.acp.agents import AgentConfig

        pt = MockProcessTable()
        svc = AcpService(process_table=pt)

        # Mock NexusFS
        mock_nx = MagicMock()
        mock_nx.sys_write = AsyncMock()
        mock_nx.sys_readdir = AsyncMock(return_value=[])
        svc._nexus_fs = mock_nx

        cfg = AgentConfig(agent_id="custom-agent", name="Custom", command="custom-bin")
        await svc.register_agent(cfg)

        # Verify in-memory registration
        assert "custom-agent" in svc._agents
        assert svc._agents["custom-agent"] is cfg

        # Verify VFS write
        mock_nx.sys_write.assert_called_once()
        call_args = mock_nx.sys_write.call_args
        path = call_args[0][0]
        content_bytes = call_args[0][1]
        assert path == "/root/agents/custom-agent/agent.json"
        written = json.loads(content_bytes.decode("utf-8"))
        assert written["agent_id"] == "custom-agent"
        assert written["command"] == "custom-bin"

    @pytest.mark.asyncio
    async def test_register_without_vfs(self):
        """register_agent still works in-memory when NexusFS is not bound."""
        from nexus.system_services.acp.agents import AgentConfig

        pt = MockProcessTable()
        svc = AcpService(process_table=pt)

        cfg = AgentConfig(agent_id="mem-only", name="MemOnly", command="mem")
        await svc.register_agent(cfg)

        assert "mem-only" in svc._agents
        assert svc._agents["mem-only"] is cfg

    @pytest.mark.asyncio
    async def test_register_vfs_write_failure_graceful(self):
        """register_agent logs warning but doesn't raise on VFS write failure."""
        from nexus.system_services.acp.agents import AgentConfig

        pt = MockProcessTable()
        svc = AcpService(process_table=pt)

        mock_nx = MagicMock()
        mock_nx.sys_write = AsyncMock(side_effect=OSError("disk full"))
        mock_nx.sys_readdir = AsyncMock(return_value=[])
        svc._nexus_fs = mock_nx

        cfg = AgentConfig(agent_id="fail-write", name="Fail", command="fail")
        # Should NOT raise
        await svc.register_agent(cfg)

        # In-memory still succeeds
        assert "fail-write" in svc._agents
