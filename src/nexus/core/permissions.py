"""Kernel-level permission types for Nexus (v0.6.0+).

This module defines the kernel-level types for ReBAC permission enforcement:
- Permission (IntFlag): permission bit flags
- OperationContext: operation context with subject identity
- check_stale_session(): stale session detection helper

PermissionEnforcer lives in services/permissions/enforcer.py — re-exported here
for backward compatibility.
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.types import OperationContext, Permission  # noqa: F401

if TYPE_CHECKING:
    from nexus.services.permissions.enforcer import PermissionEnforcer as PermissionEnforcer

logger = logging.getLogger(__name__)


def __getattr__(name: str) -> Any:
    """Lazy re-export to avoid circular import with services.permissions.enforcer."""
    if name == "PermissionEnforcer":
        from nexus.services.permissions.enforcer import PermissionEnforcer

        return PermissionEnforcer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def check_stale_session(agent_registry: Any, context: OperationContext) -> None:
    """Check for stale agent sessions and raise if the session is outdated.

    Compares the agent_generation from the JWT token (stored in context) against
    the current generation in the agent registry (DB). A mismatch means a newer
    session has superseded this one.

    Issue #1240 / #1445: Shared helper used by both sync and async enforcers.

    Args:
        agent_registry: AgentRegistry instance (or None to skip check).
        context: Operation context with agent_generation from JWT claims.

    Raises:
        StaleSessionError: If the session generation is stale or the agent
            record no longer exists (deleted agent with valid JWT).
    """
    if (
        agent_registry is None
        or context.agent_generation is None
        or context.subject_type != "agent"
    ):
        return

    agent_id = context.agent_id or context.subject_id
    if not agent_id:
        logger.warning("[STALE-SESSION] No agent_id in context, skipping check")
        return

    current_record = agent_registry.get(agent_id)

    from nexus.core.exceptions import StaleSessionError

    # Issue #1445: Agent deleted but JWT still valid → stale session
    if current_record is None:
        raise StaleSessionError(
            agent_id,
            f"Agent '{agent_id}' no longer exists (session generation "
            f"{context.agent_generation} is stale)",
        )

    if current_record.generation != context.agent_generation:
        raise StaleSessionError(
            agent_id,
            f"Session generation {context.agent_generation} is stale "
            f"(current: {current_record.generation})",
        )
