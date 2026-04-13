"""Unit tests for AgentProvisioner."""

import json

import pytest

from nexus.bricks.ipc.conventions import (
    AGENT_SUBDIRS,
    agent_card_path,
    agent_dir,
    inbox_path,
)
from nexus.bricks.ipc.provisioning import AgentProvisioner

from .fakes import InMemoryVFS

ZONE = "test-zone"


class TestAgentProvisioner:
    """Tests for auto-provisioning agent IPC directories."""

    @pytest.fixture
    def vfs(self) -> InMemoryVFS:
        return InMemoryVFS()

    @pytest.mark.asyncio
    async def test_provision_creates_directories(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision("analyst")

        assert vfs.access(agent_dir("analyst"), ZONE)
        for subdir in AGENT_SUBDIRS:
            assert vfs.access(f"{agent_dir('analyst')}/{subdir}", ZONE)

    @pytest.mark.asyncio
    async def test_provision_creates_agent_card(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision(
            "reviewer",
            name="Code Reviewer",
            skills=["code_review", "security"],
        )

        card_data = vfs.sys_read(agent_card_path("reviewer"), ZONE)
        card = json.loads(card_data)
        assert card["name"] == "Code Reviewer"
        assert card["agent_id"] == "reviewer"
        assert card["skills"] == ["code_review", "security"]
        assert card["status"] == "connected"
        assert card["inbox"] == "/agents/reviewer/inbox"
        assert "created_at" in card

    @pytest.mark.asyncio
    async def test_provision_idempotent(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision("analyst")
        await provisioner.provision("analyst")  # Should not raise

        assert vfs.access(inbox_path("analyst"), ZONE)

    @pytest.mark.asyncio
    async def test_is_provisioned(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        assert not await provisioner.is_provisioned("analyst")

        await provisioner.provision("analyst")
        assert await provisioner.is_provisioned("analyst")

    @pytest.mark.asyncio
    async def test_deprovision_marks_deprovisioned(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision("analyst")
        await provisioner.deprovision("analyst")

        card_data = vfs.sys_read(agent_card_path("analyst"), ZONE)
        card = json.loads(card_data)
        assert card["status"] == "deprovisioned"
        assert "deprovisioned_at" in card

    @pytest.mark.asyncio
    async def test_provision_with_metadata(self, vfs: InMemoryVFS) -> None:
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision(
            "custom_agent",
            metadata={"model": "claude-opus", "version": "4.5"},
        )

        card_data = vfs.sys_read(agent_card_path("custom_agent"), ZONE)
        card = json.loads(card_data)
        assert card["model"] == "claude-opus"
        assert card["version"] == "4.5"

    @pytest.mark.asyncio
    async def test_provision_creates_inbox(self, vfs: InMemoryVFS) -> None:
        """Provisioning should create the inbox directory."""
        provisioner = AgentProvisioner(vfs, zone_id=ZONE)
        await provisioner.provision("agent:bob")

        assert vfs.access(inbox_path("agent:bob"), ZONE)
