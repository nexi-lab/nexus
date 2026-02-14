"""Tests for AsyncAgentRegistry wrapper (Issue #1440)."""

from __future__ import annotations

import types
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from nexus.core.agent_record import AgentRecord, AgentState
from nexus.core.async_agent_registry import AsyncAgentRegistry, _to_agent_info
from nexus.services.protocols.agent_registry import AgentInfo, AgentRegistryProtocol
from tests.unit.core.protocols.test_conformance import assert_protocol_conformance

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record(**overrides: object) -> AgentRecord:
    """Create a test AgentRecord with sensible defaults."""
    defaults = {
        "agent_id": "agent-1",
        "owner_id": "user-1",
        "zone_id": "zone-1",
        "name": "TestAgent",
        "state": AgentState.CONNECTED,
        "generation": 1,
        "last_heartbeat": None,
        "metadata": types.MappingProxyType({}),
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return AgentRecord(**defaults)


@pytest.fixture()
def mock_inner() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def wrapper(mock_inner: MagicMock) -> AsyncAgentRegistry:
    return AsyncAgentRegistry(mock_inner)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestConformance:
    def test_assert_protocol_conformance(self) -> None:
        assert_protocol_conformance(AsyncAgentRegistry, AgentRegistryProtocol)

    def test_isinstance_check(self, wrapper: AsyncAgentRegistry) -> None:
        assert isinstance(wrapper, AgentRegistryProtocol)


# ---------------------------------------------------------------------------
# AgentRecord -> AgentInfo conversion
# ---------------------------------------------------------------------------


class TestToAgentInfo:
    def test_basic_conversion(self) -> None:
        record = _make_record()
        info = _to_agent_info(record)
        assert isinstance(info, AgentInfo)
        assert info.agent_id == "agent-1"
        assert info.owner_id == "user-1"
        assert info.zone_id == "zone-1"
        assert info.name == "TestAgent"
        assert info.state == "CONNECTED"
        assert info.generation == 1

    def test_none_fields(self) -> None:
        record = _make_record(zone_id=None, name=None)
        info = _to_agent_info(record)
        assert info.zone_id is None
        assert info.name is None

    def test_state_enum_to_string(self) -> None:
        for state in AgentState:
            record = _make_record(state=state)
            info = _to_agent_info(record)
            assert info.state == state.value


# ---------------------------------------------------------------------------
# Async method delegation
# ---------------------------------------------------------------------------


class TestRegister:
    @pytest.mark.asyncio()
    async def test_delegates_and_converts(
        self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock
    ) -> None:
        mock_inner.register.return_value = _make_record()
        info = await wrapper.register(
            "agent-1", "user-1", zone_id="z", name="A", metadata={"k": "v"}
        )
        mock_inner.register.assert_called_once_with(
            "agent-1",
            "user-1",
            zone_id="z",
            name="A",
            metadata={"k": "v"},
        )
        assert isinstance(info, AgentInfo)
        assert info.agent_id == "agent-1"


class TestGet:
    @pytest.mark.asyncio()
    async def test_found(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.get.return_value = _make_record()
        info = await wrapper.get("agent-1")
        assert info is not None
        assert info.agent_id == "agent-1"

    @pytest.mark.asyncio()
    async def test_not_found(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.get.return_value = None
        info = await wrapper.get("missing")
        assert info is None


class TestTransition:
    @pytest.mark.asyncio()
    async def test_delegates_with_state_conversion(
        self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock
    ) -> None:
        mock_inner.transition.return_value = _make_record(state=AgentState.IDLE)
        info = await wrapper.transition("agent-1", "IDLE", expected_generation=1)
        mock_inner.transition.assert_called_once_with(
            "agent-1",
            AgentState.IDLE,
            expected_generation=1,
        )
        assert info.state == "IDLE"

    @pytest.mark.asyncio()
    async def test_invalid_state_raises_value_error(
        self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock
    ) -> None:
        with pytest.raises(ValueError, match="Invalid target state"):
            await wrapper.transition("agent-1", "BOGUS")
        mock_inner.transition.assert_not_called()

    @pytest.mark.asyncio()
    async def test_propagates_exception(
        self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock
    ) -> None:
        from nexus.core.agent_registry import InvalidTransitionError

        mock_inner.transition.side_effect = InvalidTransitionError(
            "agent-1", AgentState.UNKNOWN, AgentState.IDLE
        )
        with pytest.raises(InvalidTransitionError):
            await wrapper.transition("agent-1", "IDLE")


class TestHeartbeat:
    @pytest.mark.asyncio()
    async def test_delegates(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.heartbeat.return_value = None
        await wrapper.heartbeat("agent-1")
        mock_inner.heartbeat.assert_called_once_with("agent-1")


class TestListByZone:
    @pytest.mark.asyncio()
    async def test_converts_list(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.list_by_zone.return_value = [
            _make_record(agent_id="a1"),
            _make_record(agent_id="a2"),
        ]
        result = await wrapper.list_by_zone("zone-1")
        assert len(result) == 2
        assert all(isinstance(r, AgentInfo) for r in result)
        assert result[0].agent_id == "a1"
        assert result[1].agent_id == "a2"

    @pytest.mark.asyncio()
    async def test_empty_zone(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.list_by_zone.return_value = []
        result = await wrapper.list_by_zone("empty-zone")
        assert result == []


class TestUnregister:
    @pytest.mark.asyncio()
    async def test_returns_true(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.unregister.return_value = True
        assert await wrapper.unregister("agent-1") is True

    @pytest.mark.asyncio()
    async def test_returns_false(self, wrapper: AsyncAgentRegistry, mock_inner: MagicMock) -> None:
        mock_inner.unregister.return_value = False
        assert await wrapper.unregister("missing") is False
