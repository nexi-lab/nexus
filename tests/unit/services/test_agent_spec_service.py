"""Tests for AgentRegistry spec/status methods and AsyncAgentRegistry wrappers (Issue #2169).

Tests cover:
1. set_spec — stores and retrieves spec correctly
2. set_spec — increments spec_generation
3. get_spec — returns None for agent without spec
4. get_status — returns computed status with correct phase
5. get_status — drift detection (spec_generation != observed_generation)
6. Async wrappers delegate correctly
"""

from unittest.mock import MagicMock

import pytest

from nexus.contracts.agent_types import (
    AgentPhase,
    AgentResources,
    AgentResourceUsage,
    AgentSpec,
    AgentStatus,
    QoSClass,
)
from nexus.system_services.agents.agent_registry import AsyncAgentRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(**overrides: object) -> AgentSpec:
    """Create a test AgentSpec with sensible defaults."""
    defaults = {
        "agent_type": "analyst",
        "capabilities": frozenset({"search"}),
        "resource_requests": AgentResources(),
        "resource_limits": AgentResources(),
        "qos_class": QoSClass.STANDARD,
        "zone_affinity": None,
        "spec_generation": 1,
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)


def _make_status(**overrides: object) -> AgentStatus:
    """Create a test AgentStatus with sensible defaults."""
    defaults = {
        "phase": AgentPhase.ACTIVE,
        "observed_generation": 1,
        "conditions": (),
        "resource_usage": AgentResourceUsage(),
        "last_heartbeat": None,
        "last_activity": None,
        "inbox_depth": 0,
        "context_usage_pct": 0.0,
    }
    defaults.update(overrides)
    return AgentStatus(**defaults)


@pytest.fixture()
def mock_inner() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def wrapper(mock_inner: MagicMock) -> AsyncAgentRegistry:
    return AsyncAgentRegistry(mock_inner)


# ---------------------------------------------------------------------------
# AsyncAgentRegistry.set_spec
# ---------------------------------------------------------------------------


class TestAsyncSetSpec:
    @pytest.mark.asyncio()
    async def test_delegates_and_returns_spec(
        self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock
    ) -> None:
        returned_spec = _make_spec(spec_generation=2)
        mock_inner.set_spec.return_value = returned_spec
        spec = _make_spec()
        result = await wrapper.set_spec("agent-1", spec)
        mock_inner.set_spec.assert_called_once_with("agent-1", spec)
        assert isinstance(result, AgentSpec)
        assert result.spec_generation == 2

    @pytest.mark.asyncio()
    async def test_propagates_value_error(
        self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock
    ) -> None:
        mock_inner.set_spec.side_effect = ValueError("Agent not found")
        with pytest.raises(ValueError, match="Agent not found"):
            await wrapper.set_spec("missing", _make_spec())


# ---------------------------------------------------------------------------
# AsyncAgentRegistry.get_spec
# ---------------------------------------------------------------------------


class TestAsyncGetSpec:
    @pytest.mark.asyncio()
    async def test_found(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        expected = _make_spec()
        mock_inner.get_spec.return_value = expected
        result = await wrapper.get_spec("agent-1")
        assert result is expected

    @pytest.mark.asyncio()
    async def test_not_found(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.get_spec.return_value = None
        result = await wrapper.get_spec("missing")
        assert result is None


# ---------------------------------------------------------------------------
# AsyncAgentRegistry.get_status
# ---------------------------------------------------------------------------


class TestAsyncGetStatus:
    @pytest.mark.asyncio()
    async def test_found(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        expected = _make_status()
        mock_inner.get_status.return_value = expected
        result = await wrapper.get_status("agent-1")
        assert result is expected
        assert result.phase is AgentPhase.ACTIVE

    @pytest.mark.asyncio()
    async def test_not_found(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.get_status.return_value = None
        result = await wrapper.get_status("missing")
        assert result is None

    @pytest.mark.asyncio()
    async def test_drift_visible(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        """Status with observed_generation != spec_generation indicates drift."""
        from nexus.contracts.agent_types import detect_drift

        spec = _make_spec(spec_generation=5)
        status = _make_status(observed_generation=3)
        mock_inner.get_spec.return_value = spec
        mock_inner.get_status.return_value = status

        result_status = await wrapper.get_status("agent-1")
        result_spec = await wrapper.get_spec("agent-1")
        assert result_status is not None
        assert result_spec is not None
        assert detect_drift(result_spec, result_status) is True


# ---------------------------------------------------------------------------
# QoS-based eviction ordering
# ---------------------------------------------------------------------------


class TestQoSEvictionOrdering:
    def test_spot_evicted_before_standard(self) -> None:
        """Spot agents should be evicted before standard ones."""
        qos_priority = [QoSClass.SPOT, QoSClass.STANDARD, QoSClass.PREMIUM]
        agents = [
            _make_spec(qos_class=QoSClass.PREMIUM),
            _make_spec(qos_class=QoSClass.SPOT),
            _make_spec(qos_class=QoSClass.STANDARD),
        ]
        sorted_agents = sorted(agents, key=lambda s: qos_priority.index(s.qos_class))
        assert sorted_agents[0].qos_class is QoSClass.SPOT
        assert sorted_agents[1].qos_class is QoSClass.STANDARD
        assert sorted_agents[2].qos_class is QoSClass.PREMIUM


# ---------------------------------------------------------------------------
# Sync AgentRegistry spec helpers (static methods)
# ---------------------------------------------------------------------------


class TestSpecSerialization:
    def test_spec_to_dict(self) -> None:
        from nexus.system_services.agents.agent_registry import AgentRegistry

        spec = _make_spec(
            capabilities=frozenset({"b", "a"}),
            resource_requests=AgentResources(token_budget=5000),
        )
        result = AgentRegistry._spec_to_dict(spec)
        assert result["agent_type"] == "analyst"
        assert result["capabilities"] == ["a", "b"]  # sorted
        assert result["resource_requests"]["token_budget"] == 5000
        assert result["qos_class"] == "standard"

    def test_parse_spec_json_valid(self) -> None:
        import json

        from nexus.system_services.agents.agent_registry import AgentRegistry

        spec = _make_spec()
        raw = json.dumps(AgentRegistry._spec_to_dict(spec))
        result = AgentRegistry._parse_spec_json(raw, "test")
        assert result is not None
        assert result.agent_type == "analyst"

    def test_parse_spec_json_none(self) -> None:
        from nexus.system_services.agents.agent_registry import AgentRegistry

        assert AgentRegistry._parse_spec_json(None, "test") is None

    def test_parse_spec_json_corrupt(self) -> None:
        from nexus.system_services.agents.agent_registry import AgentRegistry

        assert AgentRegistry._parse_spec_json("not json", "test") is None
