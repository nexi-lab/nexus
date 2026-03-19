"""Auto-provision IPC directories on agent registration (Issue #2037).

Hooks into agent creation events to automatically provision inbox/outbox/processed/dead_letter
directories when an agent is created, eliminating the manual provisioning step.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.ipc.provisioning import AgentProvisioner

logger = logging.getLogger(__name__)


async def auto_provision_on_agent_create(
    agent_id: str,
    provisioner: "AgentProvisioner",
) -> None:
    """Auto-provision IPC directories when an agent is created.

    Called as a hook/callback when an agent is registered.

    Args:
        agent_id: The newly created agent ID.
        provisioner: AgentProvisioner instance to create directories.
    """
    try:
        await provisioner.provision(agent_id)
        logger.info("Auto-provisioned IPC directories for agent %s", agent_id)
    except Exception as exc:
        logger.warning(
            "Failed to auto-provision IPC directories for agent %s: %s",
            agent_id,
            exc,
            exc_info=True,
        )
