from typing import Any, cast

import pytest

from nexus.contracts.process_types import AgentSignal, AgentState
from nexus.services.agents.agent_registry import AgentRegistry
from nexus.services.agents.agent_rpc_service import AgentRPCService
from nexus.services.agents.agent_warmup import AgentWarmupService
from nexus.services.agents.warmup_steps import register_standard_steps


class _ExplodingWarmupService:
    async def warmup(self, _agent_id: str):
        raise AssertionError("warmup should not be called for resumed agents")


_DUMMY = cast(Any, object())


@pytest.mark.asyncio
async def test_agent_transition_connected_bootstraps_registered_agent() -> None:
    registry = AgentRegistry()
    desc = registry.register_external(
        "agent-1",
        owner_id="alice",
        zone_id="test",
        connection_id="conn-1",
    )

    warmup_service = AgentWarmupService(agent_registry=registry)
    register_standard_steps(warmup_service)
    rpc = AgentRPCService(
        vfs=_DUMMY,
        metastore=_DUMMY,
        session_factory=lambda: None,
        agent_registry=registry,
        agent_warmup_service=warmup_service,
    )

    result = await rpc.agent_transition(
        desc.pid,
        "CONNECTED",
        expected_generation=desc.generation,
    )

    updated = registry.get(desc.pid)
    assert updated is not None
    assert updated.state is AgentState.READY
    assert result["state"] == AgentState.READY
    assert result["generation"] == updated.generation


@pytest.mark.asyncio
async def test_agent_transition_connected_resumes_suspended_agent_without_warmup() -> None:
    registry = AgentRegistry()
    desc = registry.register_external(
        "agent-2",
        owner_id="alice",
        zone_id="test",
        connection_id="conn-2",
    )
    desc = registry._transition(desc, AgentState.WARMING_UP)
    desc = registry._transition(desc, AgentState.READY)
    desc = registry.signal(desc.pid, AgentSignal.SIGSTOP)

    rpc = AgentRPCService(
        vfs=_DUMMY,
        metastore=_DUMMY,
        session_factory=lambda: None,
        agent_registry=registry,
        agent_warmup_service=_ExplodingWarmupService(),
    )

    result = await rpc.agent_transition(
        desc.pid,
        "CONNECTED",
        expected_generation=desc.generation,
    )

    updated = registry.get(desc.pid)
    assert updated is not None
    assert updated.state is AgentState.READY
    assert result["generation"] == updated.generation


@pytest.mark.asyncio
async def test_agent_transition_connected_requires_warmup_for_registered_agent() -> None:
    registry = AgentRegistry()
    desc = registry.register_external(
        "agent-3",
        owner_id="alice",
        zone_id="test",
        connection_id="conn-3",
    )
    rpc = AgentRPCService(
        vfs=_DUMMY,
        metastore=_DUMMY,
        session_factory=lambda: None,
        agent_registry=registry,
        agent_warmup_service=None,
    )

    with pytest.raises(ValueError, match="AgentWarmupService not available"):
        await rpc.agent_transition(desc.pid, "CONNECTED", expected_generation=desc.generation)
