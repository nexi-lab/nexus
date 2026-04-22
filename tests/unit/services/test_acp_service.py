"""Tests for AcpService — subprocess ownership + DT_PIPE registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.acp.service import AcpService, _ActiveAgent

# ---------------------------------------------------------------------------
# Mock AgentRegistry
# ---------------------------------------------------------------------------


@dataclass
class MockProcessDescriptor:
    pid: str = "test-pid-1"
    name: str = "acp:claude"
    owner_id: str = "user1"
    zone_id: str = ROOT_ZONE_ID
    labels: dict[str, str] = field(default_factory=dict)


class MockAgentRegistry:
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
# Tests
# ---------------------------------------------------------------------------


class TestAcpServiceConstruction:
    """Test AcpService construction and late-binding."""

    def test_init(self):
        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)
        assert svc._nexus_fs is None

    def test_bind_fs(self):
        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        mock_nx = MagicMock()
        svc.bind_fs(mock_nx)
        assert svc._nexus_fs is mock_nx


class TestAcpServiceNoTransitionHack:
    """Verify the _transition(RUNNING) hack is gone."""

    def test_no_transition_call(self):
        """AgentRegistry.spawn() returns RUNNING directly (#1691).
        _transition should not be called anywhere in service.py."""
        import inspect

        from nexus.services.acp import service

        source = inspect.getsource(service)
        assert "_transition" not in source

    def test_no_metastore_reference(self):
        """AcpService must not access metastore directly — all I/O through VFS."""
        import inspect

        from nexus.services.acp import service

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
        """kill_agent should destroy all 3 fd pipes via NexusFS.sys_unlink()."""
        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        # Teardown calls nx.sys_unlink(path) for each fd pipe.
        destroyed: list[str] = []
        mock_nx = MagicMock()
        mock_nx.sys_unlink = MagicMock(side_effect=lambda path, **kw: destroyed.append(path))
        svc.bind_fs(mock_nx)

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

        # Verify DT_PIPEs destroyed via nx.sys_unlink (all 3 fds)
        assert "/root/proc/pid-1/fd/0" in destroyed
        assert "/root/proc/pid-1/fd/1" in destroyed
        assert "/root/proc/pid-1/fd/2" in destroyed


class TestAcpServiceCloseAll:
    """Test close_all teardown."""

    def test_close_all_cleans_up(self):
        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        # Teardown goes through nx.sys_unlink post IPC Rust-ification.
        destroyed: list[str] = []
        mock_nx = MagicMock()
        mock_nx.sys_unlink = MagicMock(side_effect=lambda path, **kw: destroyed.append(path))
        svc.bind_fs(mock_nx)

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
        assert len(destroyed) == 9  # 3 pipes per agent × 3 agents


class TestAcpServiceCallAgent:
    """Test call_agent subprocess + StdioPipeBackend creation."""

    @pytest.mark.asyncio
    async def test_call_agent_reads_config_from_vfs(self):
        """Verify call_agent reads agent config from VFS (SSOT)."""
        import json

        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        # Mock NexusFS with agent config file
        agent_config = {
            "agent_id": "test-agent",
            "name": "Test",
            "command": "echo",
            "acp_args": ["hello"],
            "enabled": True,
        }

        def _mock_sys_read(path: str) -> bytes:
            if path.endswith("/agent.json"):
                return json.dumps(agent_config).encode("utf-8")
            raise FileNotFoundError(path)

        mock_nx = MagicMock()
        mock_nx.sys_read = MagicMock(side_effect=_mock_sys_read)
        mock_nx.sys_write = MagicMock()
        mock_nx.sys_readdir = MagicMock(return_value=[])
        svc.bind_fs(mock_nx)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = FileNotFoundError("test-not-found")

            result = await svc.call_agent(
                agent_id="test-agent",
                prompt="test",
                owner_id="user1",
                zone_id=ROOT_ZONE_ID,
            )

            assert result.exit_code == 127
            assert "Command not found" in result.stderr

        # Connections should be cleaned up
        assert len(svc._connections) == 0

    @pytest.mark.asyncio
    async def test_call_agent_unknown_agent_raises(self):
        """call_agent raises ValueError when agent config not in VFS."""
        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        mock_nx = MagicMock()
        mock_nx.sys_read = MagicMock(side_effect=FileNotFoundError)
        svc.bind_fs(mock_nx)

        with pytest.raises(ValueError, match="Unknown agent_id"):
            await svc.call_agent(
                agent_id="nonexistent",
                prompt="test",
                owner_id="user1",
                zone_id=ROOT_ZONE_ID,
            )


class TestAgentConfigFromDict:
    """Test AgentConfig.from_dict() deserialization from VFS JSON."""

    def test_from_dict_minimal(self):
        from nexus.services.acp.agents import AgentConfig

        data = {"agent_id": "test", "name": "Test", "command": "test-bin"}
        cfg = AgentConfig.from_dict(data)
        assert cfg.agent_id == "test"
        assert cfg.command == "test-bin"

    def test_from_dict_full(self):
        from nexus.services.acp.agents import AgentConfig

        data = {
            "agent_id": "claude",
            "name": "Claude Code",
            "command": "claude",
            "prompt_flag": "-p",
            "default_system_prompt": "You are helpful.",
            "extra_args": ["--output-format", "json"],
            "env": {"KEY": "val"},
            "npx_package": "@zed/claude-acp@1.0",
            "acp_args": ["--experimental-acp", "--dangerously-skip-permissions"],
            "enabled": True,
        }
        cfg = AgentConfig.from_dict(data)
        assert cfg.agent_id == "claude"
        assert cfg.npx_package == "@zed/claude-acp@1.0"
        assert cfg.acp_args == ("--experimental-acp", "--dangerously-skip-permissions")

    def test_from_dict_defaults(self):
        """from_dict fills in defaults for missing optional fields."""
        from nexus.services.acp.agents import AgentConfig

        data = {"agent_id": "x", "name": "X", "command": "x-bin"}
        cfg = AgentConfig.from_dict(data)
        assert cfg.prompt_flag == "-p"
        assert cfg.default_system_prompt is None
        assert cfg.extra_args == ()
        assert cfg.env == {}
        assert cfg.npx_package is None
        assert cfg.acp_args == ("--experimental-acp",)
        assert cfg.enabled is True


class TestAcpServiceReadAgentConfig:
    """Test _read_agent_config reads from VFS (SSOT)."""

    @pytest.mark.asyncio
    async def test_read_returns_config_from_vfs(self):
        """_read_agent_config reads /{zone}/agents/{id}/agent.json."""
        import json

        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        agent_json = json.dumps(
            {
                "agent_id": "claude",
                "name": "Claude Code",
                "command": "claude",
                "acp_args": ["--experimental-acp"],
            }
        ).encode()

        mock_nx = MagicMock()
        mock_nx.sys_read = MagicMock(return_value=agent_json)
        svc.bind_fs(mock_nx)

        config = await svc._read_agent_config("claude", "root")
        assert config is not None
        assert config.agent_id == "claude"
        assert config.command == "claude"
        mock_nx.sys_read.assert_called_once_with("/root/agents/claude/agent.json")

    @pytest.mark.asyncio
    async def test_read_returns_none_when_not_found(self):
        """_read_agent_config returns None for missing agent."""
        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        mock_nx = MagicMock()
        mock_nx.sys_read = MagicMock(side_effect=FileNotFoundError)
        svc.bind_fs(mock_nx)

        config = await svc._read_agent_config("nonexistent", "root")
        assert config is None

    @pytest.mark.asyncio
    async def test_read_returns_none_without_vfs(self):
        """_read_agent_config returns None when NexusFS not bound."""
        pt = MockAgentRegistry()
        svc = AcpService(agent_registry=pt)

        config = await svc._read_agent_config("claude", "root")
        assert config is None
