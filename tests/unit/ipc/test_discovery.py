"""Unit tests for AgentDiscovery."""

from __future__ import annotations

import json

import pytest

from nexus.ipc.conventions import agent_card_path
from nexus.ipc.discovery import AgentDiscovery

from .fakes import InMemoryVFS

ZONE = "test-zone"


async def _create_agent(
    vfs: InMemoryVFS,
    agent_id: str,
    name: str | None = None,
    skills: list[str] | None = None,
    status: str = "connected",
) -> None:
    """Helper: create agent directory and AGENT.json."""
    await vfs.mkdir(f"/agents/{agent_id}", ZONE)
    await vfs.mkdir(f"/agents/{agent_id}/inbox", ZONE)
    await vfs.mkdir("/agents", ZONE)
    card = {
        "name": name or agent_id,
        "agent_id": agent_id,
        "skills": skills or [],
        "status": status,
        "inbox": f"/agents/{agent_id}/inbox",
    }
    card_data = json.dumps(card).encode("utf-8")
    await vfs.write(agent_card_path(agent_id), card_data, ZONE)


class TestAgentDiscovery:
    """Tests for discovering agents via filesystem."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_list_agents(self, vfs: InMemoryVFS) -> None:
        await _create_agent(vfs, "analyst")
        await _create_agent(vfs, "reviewer")

        discovery = AgentDiscovery(vfs, zone_id=ZONE)
        agents = await discovery.list_agents()

        assert "analyst" in agents
        assert "reviewer" in agents

    @pytest.mark.asyncio
    async def test_get_agent_card(self, vfs: InMemoryVFS) -> None:
        await _create_agent(
            vfs,
            "reviewer",
            name="Code Reviewer",
            skills=["code_review", "security_audit"],
        )

        discovery = AgentDiscovery(vfs, zone_id=ZONE)
        agent = await discovery.get_agent_card("reviewer")

        assert agent is not None
        assert agent.agent_id == "reviewer"
        assert agent.name == "Code Reviewer"
        assert agent.skills == ["code_review", "security_audit"]
        assert agent.status == "connected"

    @pytest.mark.asyncio
    async def test_get_agent_card_missing(self, vfs: InMemoryVFS) -> None:
        discovery = AgentDiscovery(vfs, zone_id=ZONE)
        agent = await discovery.get_agent_card("nonexistent")
        assert agent is None

    @pytest.mark.asyncio
    async def test_discover_all(self, vfs: InMemoryVFS) -> None:
        await _create_agent(vfs, "analyst", skills=["research"])
        await _create_agent(vfs, "reviewer", skills=["code_review"])

        discovery = AgentDiscovery(vfs, zone_id=ZONE)
        agents = await discovery.discover_all()

        assert len(agents) == 2
        names = {a.agent_id for a in agents}
        assert names == {"analyst", "reviewer"}

    @pytest.mark.asyncio
    async def test_find_by_skill(self, vfs: InMemoryVFS) -> None:
        await _create_agent(vfs, "analyst", skills=["research", "data_analysis"])
        await _create_agent(vfs, "reviewer", skills=["code_review", "security_audit"])
        await _create_agent(vfs, "writer", skills=["documentation", "research"])

        discovery = AgentDiscovery(vfs, zone_id=ZONE)
        researchers = await discovery.find_by_skill("research")

        assert len(researchers) == 2
        ids = {a.agent_id for a in researchers}
        assert ids == {"analyst", "writer"}

    @pytest.mark.asyncio
    async def test_find_by_skill_no_matches(self, vfs: InMemoryVFS) -> None:
        await _create_agent(vfs, "analyst", skills=["research"])

        discovery = AgentDiscovery(vfs, zone_id=ZONE)
        results = await discovery.find_by_skill("nonexistent_skill")
        assert results == []
