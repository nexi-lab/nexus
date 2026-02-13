"""Auto-provisioning of agent IPC directories.

When an agent is registered, the provisioner creates the standard
directory layout (inbox/, outbox/, processed/, dead_letter/) and
writes an initial AGENT.json card.

Triggered by AGENT_REGISTERED events from the EventBus — zero
coupling to the AgentRegistry kernel component.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from nexus.ipc.conventions import (
    AGENT_SUBDIRS,
    agent_card_path,
    agent_dir,
    inbox_path,
)
from nexus.ipc.protocols import VFSOperations

logger = logging.getLogger(__name__)


class AgentProvisioner:
    """Creates IPC directory structure for newly registered agents.

    Args:
        vfs: VFS operations for directory and file creation.
        zone_id: Zone ID for multi-tenant isolation.
    """

    def __init__(self, vfs: VFSOperations, zone_id: str = "default") -> None:
        self._vfs = vfs
        self._zone_id = zone_id

    async def provision(
        self,
        agent_id: str,
        name: str | None = None,
        skills: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Provision IPC directories and agent card for a new agent.

        Creates:
        - ``/agents/{agent_id}/`` (root)
        - ``/agents/{agent_id}/inbox/``
        - ``/agents/{agent_id}/outbox/``
        - ``/agents/{agent_id}/processed/``
        - ``/agents/{agent_id}/dead_letter/``
        - ``/agents/{agent_id}/AGENT.json``

        Idempotent — safe to call multiple times for the same agent.

        Args:
            agent_id: Unique agent identifier.
            name: Human-readable name (defaults to agent_id).
            skills: List of skill/capability identifiers.
            metadata: Additional metadata for the agent card.
        """
        root = agent_dir(agent_id)

        # Create root and subdirectories
        await self._vfs.mkdir(root, self._zone_id)
        for subdir in AGENT_SUBDIRS:
            await self._vfs.mkdir(f"{root}/{subdir}", self._zone_id)

        # Write AGENT.json card
        card = {
            "name": name or agent_id,
            "agent_id": agent_id,
            "skills": skills or [],
            "status": "connected",
            "inbox": inbox_path(agent_id),
            "created_at": datetime.now(UTC).isoformat(),
            **(metadata or {}),
        }
        card_data = json.dumps(card, indent=2).encode("utf-8")
        card_file = agent_card_path(agent_id)
        await self._vfs.write(card_file, card_data, self._zone_id)

        logger.info(
            "Provisioned IPC directories for agent %s (%d subdirs + AGENT.json)",
            agent_id,
            len(AGENT_SUBDIRS),
        )

    async def deprovision(self, agent_id: str) -> None:
        """Remove IPC directories for an unregistered agent.

        Note: This does NOT delete messages — it only removes the
        agent card. Messages in inbox/outbox/processed/dead_letter
        are preserved for audit purposes. A separate cleanup policy
        should handle message retention.

        Args:
            agent_id: The agent to deprovision.
        """
        card_file = agent_card_path(agent_id)
        try:
            # Update card status to "deprovisioned" instead of deleting
            card_data = json.dumps(
                {
                    "name": agent_id,
                    "agent_id": agent_id,
                    "status": "deprovisioned",
                    "deprovisioned_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ).encode("utf-8")
            await self._vfs.write(card_file, card_data, self._zone_id)
            logger.info("Deprovisioned IPC for agent %s", agent_id)
        except Exception:
            logger.warning(
                "Failed to deprovision IPC for agent %s",
                agent_id,
                exc_info=True,
            )

    async def is_provisioned(self, agent_id: str) -> bool:
        """Check if an agent has been provisioned for IPC.

        Args:
            agent_id: The agent to check.

        Returns:
            True if the agent's inbox directory exists.
        """
        return await self._vfs.exists(inbox_path(agent_id), self._zone_id)
