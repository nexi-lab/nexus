"""Pure utility functions for agent context extraction and config creation.

Extracted from AgentRPCService (Issue #2133) to break the
core/ -> services/ import dependency. These are stateless functions
with no service-layer dependencies.
"""

import contextlib
import json
import logging
from typing import Any

from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


def compute_agent_path(agent_id: str, user_id: str, zone_id: str) -> str:
    """Compute the NexusFS directory path for an agent.

    Agent IDs may contain a comma-separated prefix (e.g. "user_id,agent_name").
    This extracts the agent name part and builds the canonical path.

    Returns:
        Path like ``/zone/{zone_id}/user/{user_id}/agent/{agent_name}``.
    """
    agent_name_part = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
    return f"/zone/{zone_id}/user/{user_id}/agent/{agent_name_part}"


def parse_entity_metadata(entity_metadata_str: str | None) -> dict[str, Any]:
    """Safely parse JSON entity metadata string into a dict.

    Returns an empty dict on None input or parse failure.
    """
    if not entity_metadata_str:
        return {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        result = json.loads(entity_metadata_str)
        if isinstance(result, dict):
            return result
    return {}


def extract_zone_id(context: dict[str, Any] | Any | None) -> str | None:
    """Extract zone_id from an operation context (dict or object)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("zone_id")
    return getattr(context, "zone_id", None)


def extract_user_id(context: dict[str, Any] | Any | None) -> str | None:
    """Extract user_id from an operation context (dict or object)."""
    if not context:
        return None
    if isinstance(context, dict):
        return context.get("user_id")
    return getattr(context, "user_id", None)


def create_agent_config_data(
    agent_id: str,
    name: str,
    user_id: str,
    description: str | None,
    created_at: str | None,
    metadata: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Build agent config data dictionary."""
    config_data: dict[str, Any] = {
        "agent_id": agent_id,
        "name": name,
        "user_id": user_id,
        "description": description,
        "created_at": created_at,
    }
    if metadata:
        config_data["metadata"] = metadata.copy()
    if api_key is not None:
        config_data["api_key"] = api_key
    return config_data


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

    from nexus.contracts.exceptions import StaleSessionError

    # Issue #1445: Agent deleted but JWT still valid -> stale session
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
