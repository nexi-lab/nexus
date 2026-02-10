"""Agent provisioning utilities for user setup.

Provides functions to create and configure standard agent types
(ImpersonatedUser, UntrustedAgent) with consistent configuration.
"""

import logging
import os
from typing import Any, cast

logger = logging.getLogger(__name__)


# Default agent configuration metadata
def get_default_agent_metadata() -> dict[str, Any]:
    """Get default agent metadata with configurable URLs from environment."""
    return {
        "platform": "langgraph",
        "endpoint_url": os.getenv("LANGGRAPH_SERVER_URL", "http://localhost:2024"),
        "nexus_server_url": os.getenv("NEXUS_SERVER_URL")
        or os.getenv("NEXUS_URL", "http://localhost:2026"),
        "agent_id": "agent",
    }


# For backwards compatibility
DEFAULT_AGENT_METADATA = get_default_agent_metadata()


def create_impersonated_user_agent(
    nx: Any, user_id: str, context: Any, metadata: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Create an ImpersonatedUser agent (digital twin).

    This agent has no separate identity and inherits all user permissions.
    It does not have an API key and acts as a digital twin of the user.

    Args:
        nx: NexusFS instance
        user_id: User ID to create agent for
        context: Operation context for the user
        metadata: Optional agent metadata (uses get_default_agent_metadata() if not provided)

    Returns:
        Agent creation result dict, or None on failure

    Examples:
        >>> result = create_impersonated_user_agent(nx, "alice", alice_context)
        >>> print(result.get('config_path'))
        /zone/default/user/alice/agent/ImpersonatedUser/config.yaml
    """
    agent_metadata = metadata or get_default_agent_metadata()
    agent_id = f"{user_id},ImpersonatedUser"

    try:
        agent_result = nx.register_agent(
            agent_id=agent_id,
            name="ImpersonatedUser",
            description="Digital twin agent - no separate identity, inherits all user permissions",
            generate_api_key=False,  # No API key - uses user's auth
            metadata=agent_metadata,
            context=context,
        )
        logger.info(
            f"Created agent 'ImpersonatedUser' (digital twin) at {agent_result.get('config_path', 'N/A')}"
        )
        return cast(dict[str, Any], agent_result)
    except Exception as e:
        logger.error(f"Failed to create ImpersonatedUser agent: {e}")
        return None


def create_untrusted_agent(
    nx: Any, user_id: str, context: Any, metadata: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Create an UntrustedAgent with API key and zero default permissions.

    This agent has its own API key and zero permissions by default.
    Permissions must be explicitly granted (typically read-only viewer access).

    Args:
        nx: NexusFS instance
        user_id: User ID to create agent for
        context: Operation context for the user
        metadata: Optional agent metadata (uses get_default_agent_metadata() if not provided)

    Returns:
        Agent creation result dict, or None on failure

    Examples:
        >>> result = create_untrusted_agent(nx, "alice", alice_context)
        >>> print(result.get('api_key'))
        sk-alice,UntrustedAgent_12345...
    """
    agent_metadata = metadata or get_default_agent_metadata()
    agent_id = f"{user_id},UntrustedAgent"

    try:
        agent_result = nx.register_agent(
            agent_id=agent_id,
            name="UntrustedAgent",
            description="Untrusted agent with API key - zero permissions by default, read-only access granted explicitly",
            generate_api_key=True,  # Has its own API key
            metadata=agent_metadata,
            context=context,
        )
        logger.info(
            f"Created agent 'UntrustedAgent' (with API key, zero permissions) at {agent_result.get('config_path', 'N/A')}"
        )
        return cast(dict[str, Any], agent_result)
    except Exception as e:
        logger.error(f"Failed to create UntrustedAgent agent: {e}")
        return None


def create_skill_builder_agent(
    nx: Any, user_id: str, context: Any, metadata: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Create a SkillBuilder agent with API key for skill creation.

    This agent has its own API key and is granted viewer access to:
    - The skill-creator skill
    - The user's resource folder

    Args:
        nx: NexusFS instance
        user_id: User ID to create agent for
        context: Operation context for the user
        metadata: Optional agent metadata (uses get_default_agent_metadata() if not provided)

    Returns:
        Agent creation result dict, or None on failure

    Examples:
        >>> result = create_skill_builder_agent(nx, "alice", alice_context)
        >>> print(result.get('api_key'))
        sk-alice,SkillBuilder_12345...
    """
    agent_metadata = metadata or get_default_agent_metadata()
    agent_id = f"{user_id},SkillBuilder"

    try:
        agent_result = nx.register_agent(
            agent_id=agent_id,
            name="SkillBuilder",
            description="Skill builder agent with API key - has read access to skill-creator skill and resource folder",
            generate_api_key=True,  # Has its own API key
            metadata=agent_metadata,
            context=context,
        )
        logger.info(
            f"Created agent 'SkillBuilder' (with API key, skill creation access) at {agent_result.get('config_path', 'N/A')}"
        )
        return cast(dict[str, Any], agent_result)
    except Exception as e:
        logger.error(f"Failed to create SkillBuilder agent: {e}")
        return None


def create_standard_agents(
    nx: Any, user_id: str, context: Any, metadata: dict[str, Any] | None = None
) -> dict[str, dict[str, Any] | None]:
    """Create all standard agent types (ImpersonatedUser, UntrustedAgent, and SkillBuilder).

    Convenience function to create all agents with a single call.

    Args:
        nx: NexusFS instance
        user_id: User ID to create agents for
        context: Operation context for the user
        metadata: Optional agent metadata (uses get_default_agent_metadata() if not provided)

    Returns:
        Dictionary with 'impersonated', 'untrusted', and 'skill_builder' keys containing results

    Examples:
        >>> results = create_standard_agents(nx, "alice", alice_context)
        >>> if results['impersonated']:
        ...     print("Digital twin created successfully")
        >>> if results['untrusted']:
        ...     print(f"API key: {results['untrusted'].get('api_key')}")
        >>> if results['skill_builder']:
        ...     print(f"SkillBuilder API key: {results['skill_builder'].get('api_key')}")
    """
    return {
        "impersonated": create_impersonated_user_agent(nx, user_id, context, metadata),
        "untrusted": create_untrusted_agent(nx, user_id, context, metadata),
        "skill_builder": create_skill_builder_agent(nx, user_id, context, metadata),
    }


def grant_agent_resource_access(
    nx: Any,
    user_id: str,
    zone_id: str,
    resource_types: list[str],
    agent_name: str = "UntrustedAgent",
) -> int:
    """Grant viewer (read-only) permissions to agent for specified resource types.

    Args:
        nx: NexusFS instance
        user_id: User ID who owns the resources
        zone_id: Zone ID
        resource_types: List of resource type names to grant access to
        agent_name: Agent name (default: "UntrustedAgent")

    Returns:
        Number of successful permission grants

    Examples:
        >>> granted = grant_agent_resource_access(
        ...     nx, "alice", "default", ["resource", "workspace"]
        ... )
        >>> print(f"Granted {granted} permissions")
        Granted 2 permissions
    """
    agent_id = f"{user_id},{agent_name}"
    user_base_path = f"/zone/{zone_id}/user/{user_id}"
    granted_count = 0

    for resource_type in resource_types:
        folder_path = f"{user_base_path}/{resource_type}"
        try:
            nx.rebac_create(
                subject=("agent", agent_id),
                relation="viewer",  # Read-only access
                object=("file", folder_path),
                zone_id=zone_id,
            )
            logger.info(f"Granted viewer permission on {folder_path} to {agent_name}")
            granted_count += 1
        except Exception as e:
            logger.warning(f"Failed to grant permission on {folder_path}: {e}")

    return granted_count


def grant_skill_builder_permissions(
    nx: Any,
    user_id: str,
    zone_id: str,
) -> int:
    """Grant SkillBuilder agent permissions to skill-creator skill, resource folder, and workspace.

    This grants the SkillBuilder agent:
    - Read-only access to the skill-creator skill (for skill creation capabilities)
    - Read-only access to the user's resource folder (for accessing resources during skill building)
    - Editor access to the user's workspace (for creating and modifying skill files)

    Args:
        nx: NexusFS instance
        user_id: User ID who owns the agent
        zone_id: Zone ID

    Returns:
        Number of successful permission grants

    Examples:
        >>> granted = grant_skill_builder_permissions(nx, "alice", "default")
        >>> print(f"Granted {granted} permissions to SkillBuilder")
        Granted 3 permissions to SkillBuilder
    """
    agent_id = f"{user_id},SkillBuilder"
    user_base_path = f"/zone/{zone_id}/user/{user_id}"
    granted_count = 0

    # Grant access to skill-creator skill
    skill_creator_path = f"{user_base_path}/skill/skill-creator"
    try:
        nx.rebac_create(
            subject=("agent", agent_id),
            relation="direct_viewer",  # Read-only access
            object=("file", skill_creator_path),
            zone_id=zone_id,
        )
        logger.info(f"Granted viewer permission on {skill_creator_path} to SkillBuilder")
        granted_count += 1
    except Exception as e:
        logger.warning(f"Failed to grant permission on {skill_creator_path}: {e}")

    # Grant access to resource folder
    resource_path = f"{user_base_path}/resource"
    try:
        nx.rebac_create(
            subject=("agent", agent_id),
            relation="direct_viewer",  # Read-only access
            object=("file", resource_path),
            zone_id=zone_id,
        )
        logger.info(f"Granted viewer permission on {resource_path} to SkillBuilder")
        granted_count += 1
    except Exception as e:
        logger.warning(f"Failed to grant permission on {resource_path}: {e}")

    # Grant editor access to workspace folder
    workspace_path = f"{user_base_path}/workspace"
    try:
        nx.rebac_create(
            subject=("agent", agent_id),
            relation="direct_editor",  # Editor access for creating/modifying skills
            object=("file", workspace_path),
            zone_id=zone_id,
        )
        logger.info(f"Granted editor permission on {workspace_path} to SkillBuilder")
        granted_count += 1
    except Exception as e:
        logger.warning(f"Failed to grant permission on {workspace_path}: {e}")

    return granted_count
