"""AgentRPCService lifecycle behavior coverage for issue #4137."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.contracts.process_types import AgentSignal, AgentState, InvalidTransitionError
from nexus.services.agents.agent_rpc_service import AgentRPCService


@dataclass
class _Descriptor:
    pid: str = "alice"
    state: AgentState = AgentState.READY
    generation: int = 7


class _Registry:
    def __init__(self, desc: _Descriptor | None = None) -> None:
        self.desc = desc or _Descriptor()
        self.heartbeats: list[str] = []
        self.signals: list[tuple[str, AgentSignal]] = []

    def heartbeat(self, agent_id: str) -> None:
        self.heartbeats.append(agent_id)

    def get(self, agent_id: str) -> _Descriptor | None:
        if agent_id == self.desc.pid:
            return self.desc
        return None

    def signal(self, agent_id: str, signal: AgentSignal) -> _Descriptor:
        self.signals.append((agent_id, signal))
        self.desc.generation += 1
        if signal == AgentSignal.SIGCONT:
            self.desc.state = AgentState.BUSY
        elif signal == AgentSignal.SIGSTOP:
            self.desc.state = AgentState.SUSPENDED
        return self.desc


@pytest.fixture()
def rpc() -> tuple[AgentRPCService, _Registry]:
    registry = _Registry()
    service = AgentRPCService(
        vfs=MagicMock(),
        metastore=MagicMock(),
        session_factory=MagicMock(),
        agent_registry=registry,
    )
    return service, registry


def test_list_agents_returns_entity_registry_rows() -> None:
    entity_registry = MagicMock()
    entity_registry.get_entities_by_type.return_value = [
        SimpleNamespace(
            entity_id="alice",
            parent_id="alice-owner",
            entity_metadata=json.dumps({"name": "Alice Bot"}),
            created_at=datetime(2026, 5, 21, tzinfo=UTC),
        )
    ]
    session = MagicMock()
    session.scalars.return_value.all.return_value = [
        SimpleNamespace(subject_id="alice", inherit_permissions=False)
    ]
    service = AgentRPCService(
        vfs=MagicMock(),
        metastore=MagicMock(),
        session_factory=MagicMock(return_value=session),
        entity_registry=entity_registry,
        agent_registry=_Registry(),
    )

    result = service.list_agents()

    assert result == [
        {
            "agent_id": "alice",
            "user_id": "alice-owner",
            "name": "Alice Bot",
            "created_at": "2026-05-21T00:00:00+00:00",
            "has_api_key": True,
            "inherit_permissions": False,
        }
    ]


def test_agent_heartbeat_records_liveness(rpc: tuple[AgentRPCService, _Registry]) -> None:
    service, registry = rpc

    result = service.agent_heartbeat("alice")

    assert result == {"ok": True}
    assert registry.heartbeats == ["alice"]


@pytest.mark.asyncio()
async def test_agent_transition_rejects_stale_generation(
    rpc: tuple[AgentRPCService, _Registry],
) -> None:
    service, _registry = rpc

    with pytest.raises(InvalidTransitionError, match="stale generation"):
        await service.agent_transition("alice", "IDLE", expected_generation=99)


@pytest.mark.asyncio()
async def test_agent_transition_signals_target_state(
    rpc: tuple[AgentRPCService, _Registry],
) -> None:
    service, registry = rpc

    result = await service.agent_transition("alice", "IDLE", expected_generation=7)

    assert registry.signals == [("alice", AgentSignal.SIGSTOP)]
    assert result["agent_id"] == "alice"
    assert result["generation"] == 8


@pytest.mark.asyncio()
async def test_register_agent_persists_entity_registry_row() -> None:
    kernel = MagicMock()
    kernel.sys_stat.return_value = None
    registry = MagicMock()
    registry.register_external.return_value = SimpleNamespace(
        state=AgentState.REGISTERED,
        generation=1,
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_000,
    )
    entity_registry = MagicMock()
    entity_registry.get_entity.return_value = None
    service = AgentRPCService(
        vfs=MagicMock(),
        metastore=kernel,
        session_factory=MagicMock(),
        agent_registry=registry,
        entity_registry=entity_registry,
    )

    result = await service.register_agent(
        agent_id="admin,bob",
        name="Bob",
        description="Test agent",
        metadata={"issue": "4137"},
        context={"user_id": "admin", "zone_id": "root"},
    )

    assert result["agent_id"] == "admin,bob"
    entity_registry.register_entity.assert_called_once_with(
        entity_type="agent",
        entity_id="admin,bob",
        parent_type="user",
        parent_id="admin",
        entity_metadata={
            "name": "Bob",
            "zone_id": "root",
            "description": "Test agent",
            "metadata": {"issue": "4137"},
        },
    )


@pytest.mark.asyncio()
async def test_register_agent_cleans_registry_rows_when_late_side_effect_fails() -> None:
    kernel = MagicMock()
    kernel.sys_stat.return_value = None
    registry = MagicMock()
    registry.register_external.return_value = SimpleNamespace(
        state=AgentState.REGISTERED,
        generation=1,
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_000,
    )
    entity_registry = MagicMock()
    entity_registry.get_entity.return_value = None
    service = AgentRPCService(
        vfs=MagicMock(),
        metastore=kernel,
        session_factory=MagicMock(),
        agent_registry=registry,
        entity_registry=entity_registry,
    )

    with pytest.raises(RuntimeError, match="API key creator not injected"):
        await service.register_agent(
            agent_id="admin,bob",
            name="Bob",
            generate_api_key=True,
            context={"user_id": "admin", "zone_id": "root"},
        )

    registry.unregister_external.assert_called_once_with("admin,bob")
    entity_registry.delete_entity.assert_called_once_with("agent", "admin,bob")


@pytest.mark.asyncio()
async def test_delete_agent_removes_entity_when_process_registry_entry_is_missing() -> None:
    session = MagicMock()
    session.execute.return_value = SimpleNamespace(rowcount=0)
    registry = MagicMock()
    registry.unregister_external.side_effect = NexusFileNotFoundError("admin,bob")
    entity_registry = MagicMock()
    service = AgentRPCService(
        vfs=MagicMock(),
        metastore=MagicMock(),
        session_factory=MagicMock(return_value=session),
        agent_registry=registry,
        entity_registry=entity_registry,
    )

    result = await service.delete_agent(
        "admin,bob",
        {"user_id": "admin", "zone_id": "root"},
    )

    assert result is True
    registry.unregister_external.assert_called_once_with("admin,bob")
    entity_registry.delete_entity.assert_called_once_with("agent", "admin,bob")
