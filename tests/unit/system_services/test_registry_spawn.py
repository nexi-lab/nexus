"""Tests for registry-based agent spawn (Issue #2761).

Verifies:
    - spawn(agent_id=...) looks up AgentSpec from registry
    - spawn raises AgentNotFoundError for unknown agents
    - spawn raises AgentNotFoundError when no registry is configured
    - spawn(config=...) bypasses registry (backward compat)
    - _spec_to_config correctly maps AgentSpec fields
    - spawn by agent_id registers the new process
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.agent_runtime_types import AgentNotFoundError
from nexus.contracts.agent_types import AgentResources, AgentSpec, QoSClass
from nexus.contracts.protocols.agent_vfs import AgentVFSProtocol
from nexus.system_services.agent_runtime.process_manager import ProcessManager, _spec_to_config
from nexus.system_services.agent_runtime.types import AgentProcessConfig

# ======================================================================
# Helpers
# ======================================================================


def _make_vfs() -> MagicMock:
    vfs = MagicMock(spec=AgentVFSProtocol)
    vfs.sys_read = MagicMock(return_value=b"content")
    vfs.sys_write = MagicMock(return_value=42)
    vfs.sys_access = MagicMock(return_value=True)
    vfs.sys_readdir = MagicMock(return_value=[])
    return vfs


def _make_spec(**overrides: object) -> AgentSpec:
    defaults = {
        "agent_type": "coder",
        "capabilities": frozenset({"code", "search"}),
        "resource_requests": AgentResources(),
        "resource_limits": AgentResources(),
        "qos_class": QoSClass.STANDARD,
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a coder.",
        "tools": ("read_file", "write_file", "bash"),
        "max_turns": 50,
        "max_context_tokens": 128_000,
        "sandbox_timeout": 120,
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)


def _make_registry(spec: AgentSpec | None = None) -> AsyncMock:
    registry = AsyncMock()
    registry.get_spec = AsyncMock(return_value=spec)
    registry.register = AsyncMock()
    registry.unregister = AsyncMock(return_value=True)
    return registry


# ======================================================================
# _spec_to_config
# ======================================================================


class TestSpecToConfig:
    """Verify _spec_to_config maps AgentSpec fields to AgentProcessConfig."""

    def test_spec_to_config_maps_fields(self) -> None:
        spec = _make_spec(
            model="claude-opus-4-6",
            max_turns=25,
            sandbox_timeout=60,
        )

        config = _spec_to_config(spec, name="my-agent")

        assert config.name == "my-agent"
        assert config.agent_type == "coder"
        assert config.model == "claude-opus-4-6"
        assert config.system_prompt == "You are a coder."
        assert config.tools == ("read_file", "write_file", "bash")
        assert config.max_turns == 25
        assert config.max_context_tokens == 128_000
        assert config.sandbox_timeout == 60
        assert config.qos_class == QoSClass.STANDARD

    def test_spec_to_config_none_prompt(self) -> None:
        spec = _make_spec(system_prompt=None)
        config = _spec_to_config(spec, name="no-prompt")
        assert config.system_prompt is None


# ======================================================================
# Registry-based spawn
# ======================================================================


class TestRegistrySpawn:
    """Test spawn(agent_id=...) with registry lookup."""

    async def test_spawn_by_agent_id_looks_up_registry(self) -> None:
        """spawn(agent_id=...) calls registry.get_spec() and uses result."""
        spec = _make_spec()
        registry = _make_registry(spec)
        vfs = _make_vfs()
        llm = AsyncMock()

        pm = ProcessManager(vfs, llm, agent_registry=registry)
        proc = await pm.spawn("owner-1", "zone-1", agent_id="my-coder")

        registry.get_spec.assert_called_once_with("my-coder")
        assert proc.name == "my-coder"
        assert proc.model == "claude-sonnet-4-6"

    async def test_spawn_agent_not_found_raises(self) -> None:
        """spawn(agent_id=...) raises AgentNotFoundError if spec is None."""
        registry = _make_registry(None)  # get_spec returns None
        vfs = _make_vfs()
        llm = AsyncMock()

        pm = ProcessManager(vfs, llm, agent_registry=registry)

        with pytest.raises(AgentNotFoundError, match="not-registered"):
            await pm.spawn("owner-1", "zone-1", agent_id="not-registered")

    async def test_spawn_no_registry_raises(self) -> None:
        """spawn(agent_id=...) without registry raises AgentNotFoundError."""
        vfs = _make_vfs()
        llm = AsyncMock()

        pm = ProcessManager(vfs, llm)  # no registry

        with pytest.raises(AgentNotFoundError, match="orphan-agent"):
            await pm.spawn("owner-1", "zone-1", agent_id="orphan-agent")

    async def test_spawn_with_config_skips_registry(self) -> None:
        """spawn(config=...) ignores agent_id and does not call registry."""
        registry = _make_registry(_make_spec())
        vfs = _make_vfs()
        llm = AsyncMock()

        pm = ProcessManager(vfs, llm, agent_registry=registry)
        config = AgentProcessConfig(name="inline-agent")
        proc = await pm.spawn("owner-1", "zone-1", config=config)

        registry.get_spec.assert_not_called()
        assert proc.name == "inline-agent"

    async def test_spawn_neither_config_nor_agent_id_raises(self) -> None:
        """spawn() without config or agent_id raises ValueError."""
        vfs = _make_vfs()
        llm = AsyncMock()
        pm = ProcessManager(vfs, llm)

        with pytest.raises(ValueError, match="Either agent_id or config"):
            await pm.spawn("owner-1", "zone-1")

    async def test_spawn_by_id_registers_with_registry(self) -> None:
        """Registry-based spawn still registers the new process."""
        spec = _make_spec()
        registry = _make_registry(spec)
        vfs = _make_vfs()
        llm = AsyncMock()

        pm = ProcessManager(vfs, llm, agent_registry=registry)
        proc = await pm.spawn("owner-1", "zone-1", agent_id="registered-agent")

        # registry.register is called with the new PID
        registry.register.assert_called_once()
        call_kwargs = registry.register.call_args
        assert call_kwargs.kwargs["agent_id"] == proc.pid
        assert call_kwargs.kwargs["owner_id"] == "owner-1"
        assert call_kwargs.kwargs["zone_id"] == "zone-1"
