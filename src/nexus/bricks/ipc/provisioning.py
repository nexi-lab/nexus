"""Auto-provisioning of agent IPC directories.

When an agent is registered, the provisioner creates the standard
directory layout (inbox/, outbox/, processed/, dead_letter/) and
writes an initial AGENT.json card.

Issue #3197: Optionally creates a DT_PIPE notification pipe via
``NotifyPipeFactory`` for same-node wakeup signaling.

Triggered by AGENT_REGISTERED events from the EventBus.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from nexus.bricks.ipc.conventions import (
    AGENT_SUBDIRS,
    agent_card_path,
    agent_dir,
    inbox_path,
)
from nexus.bricks.ipc.protocols import NotifyPipeFactory, VFSOperations
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class AgentProvisioner:
    """Creates IPC directory structure for newly registered agents.

    Args:
        storage: Storage driver for IPC directory and file creation.
        zone_id: Zone ID for multi-zone isolation.
        notify_pipe_factory: Factory for creating DT_PIPE notification pipes. Optional.
    """

    def __init__(
        self,
        storage: VFSOperations,
        zone_id: str = ROOT_ZONE_ID,
        notify_pipe_factory: NotifyPipeFactory | None = None,
    ) -> None:
        self._storage = storage
        self._zone_id = zone_id
        self._notify_pipe_factory = notify_pipe_factory

    async def provision(
        self,
        agent_id: str,
        name: str | None = None,
        skills: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Provision IPC directories, agent card, and notify pipe for a new agent.

        Creates:
        - ``/agents/{agent_id}/`` (root)
        - ``/agents/{agent_id}/inbox/``
        - ``/agents/{agent_id}/outbox/``
        - ``/agents/{agent_id}/processed/``
        - ``/agents/{agent_id}/dead_letter/``
        - ``/agents/{agent_id}/AGENT.json``
        - ``/agents/{agent_id}/notify`` (DT_PIPE, if factory provided)

        Idempotent — safe to call multiple times for the same agent.

        Args:
            agent_id: Unique agent identifier.
            name: Human-readable name (defaults to agent_id).
            skills: List of skill/capability identifiers.
            metadata: Additional metadata for the agent card.
        """
        root = agent_dir(agent_id)

        # Create root and subdirectories
        await self._storage.sys_mkdir(root, self._zone_id)
        for subdir in AGENT_SUBDIRS:
            await self._storage.sys_mkdir(f"{root}/{subdir}", self._zone_id)

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
        await self._storage.write(card_file, card_data, self._zone_id)

        # Create DT_PIPE notification pipe (if factory provided)
        if self._notify_pipe_factory is not None:
            try:
                self._notify_pipe_factory.create_notify_pipe(agent_id)
            except Exception:
                logger.warning(
                    "Failed to create notify pipe for agent %s (non-fatal)",
                    agent_id,
                    exc_info=True,
                )

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
            await self._storage.write(card_file, card_data, self._zone_id)
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
        return await self._storage.sys_access(inbox_path(agent_id), self._zone_id)
