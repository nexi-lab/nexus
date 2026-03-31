from unittest.mock import MagicMock

from nexus.contracts.process_types import AgentState
from nexus.core.agent_registry import AgentRegistry
from nexus.services.agents.agent_rpc_service import AgentRPCService


def _make_service(agent_registry):
    return AgentRPCService(
        vfs=MagicMock(),
        metastore=MagicMock(),
        session_factory=MagicMock(),
        agent_registry=agent_registry,
    )


def test_agent_transition_connected_bootstraps_registered_external_agent():
    registry = AgentRegistry()
    service = _make_service(registry)
    desc = registry.register_external(
        "rpc-agent",
        owner_id="alice",
        zone_id="test",
        connection_id="conn-rpc-agent",
    )

    result = service.agent_transition(
        desc.pid,
        "CONNECTED",
        expected_generation=desc.generation,
    )

    assert result["state"] == str(AgentState.READY)
    assert result["generation"] == desc.generation + 1
