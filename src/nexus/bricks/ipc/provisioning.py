"""Auto-provisioning of agent IPC directories.

When an agent is registered, the provisioner creates the standard
directory layout (inbox/, outbox/, processed/, dead_letter/) and
writes an initial AGENT.json card.

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
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class AgentProvisioner:
    """Creates IPC directory structure for newly registered agents.

    Args:
        vfs: NexusFS instance for IPC directory and file creation.
        zone_id: Zone ID for multi-zone isolation.
    """

    def __init__(
        self,
        vfs: Any,
        zone_id: str = ROOT_ZONE_ID,
    ) -> None:
        self._vfs = vfs
        self._zone_id = zone_id

    @property
    def zone_id(self) -> str:
        """Public accessor for the provisioner's zone id.

        Consumers (lifespan wiring, TTL sweeper, federation) use this to
        derive their own zone context so all IPC paths agree on one zone.
        """
        return self._zone_id

    def _ctx(self) -> Any:
        from nexus.contracts.types import OperationContext

        return OperationContext(user_id="system", groups=[], zone_id=self._zone_id, is_system=True)

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
        self._vfs.mkdir(root, parents=True, exist_ok=True, context=self._ctx())
        for subdir in AGENT_SUBDIRS:
            self._vfs.mkdir(f"{root}/{subdir}", parents=True, exist_ok=True, context=self._ctx())

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
        self._vfs.write(card_file, card_data, context=self._ctx())

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
            self._vfs.write(card_file, card_data, context=self._ctx())
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
        result: bool = self._vfs.access(inbox_path(agent_id), context=self._ctx())
        return result
