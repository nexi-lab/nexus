"""
Agent management utilities for provisioning operations.

Provides functions to create and configure standard agent types
(ImpersonatedUser, UntrustedAgent) with consistent configuration.
"""

from typing import Any, cast

# Default agent configuration metadata
DEFAULT_AGENT_METADATA = {
    "platform": "langgraph",
    "endpoint_url": "http://localhost:2024",
    "agent_id": "agent",
}


def create_impersonated_user_agent(
    nx: Any, user_id: str, context: Any, metadata: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Create an ImpersonatedUser agent (digital twin).

    This agent has no separate identity and inherits all user permissions.
    It does not have an API key and acts as a digital twin of the user.

    Args:
        nx: NexusFS instance
        user_id: User ID to create agent for
        context: Operation context for the user
        metadata: Optional agent metadata (uses DEFAULT_AGENT_METADATA if not provided)

    Returns:
        Agent creation result dict, or None on failure

    Examples:
        >>> result = create_impersonated_user_agent(nx, "alice", alice_context)
        >>> print(result.get('config_path'))
        /tenant:default/user:alice/agent/alice,ImpersonatedUser/config.json
    """
    agent_metadata = metadata or DEFAULT_AGENT_METADATA
    agent_id = f"{user_id},ImpersonatedUser"

    try:
        agent_result = cast(
            dict[str, Any],
            nx.register_agent(
                agent_id=agent_id,
                name="ImpersonatedUser",
                description="Digital twin agent - no separate identity, inherits all user permissions",
                generate_api_key=False,  # No API key - uses user's auth
                inherit_permissions=True,  # Inherit all user's permissions
                metadata=agent_metadata,
                context=context,
            ),
        )
        print(
            f"  ✓ Created agent 'ImpersonatedUser' (digital twin) at {agent_result.get('config_path', 'N/A')}"
        )
        return agent_result
    except Exception as e:
        print(f"  ✗ Failed to create ImpersonatedUser agent: {e}")
        return None


def create_untrusted_agent(
    nx: Any, user_id: str, context: Any, metadata: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Create an UntrustedAgent with API key and zero default permissions.

    This agent has its own API key and zero permissions by default.
    Permissions must be explicitly granted (typically read-only viewer access).

    Args:
        nx: NexusFS instance
        user_id: User ID to create agent for
        context: Operation context for the user
        metadata: Optional agent metadata (uses DEFAULT_AGENT_METADATA if not provided)

    Returns:
        Agent creation result dict, or None on failure

    Examples:
        >>> result = create_untrusted_agent(nx, "alice", alice_context)
        >>> print(result.get('api_key'))
        sk-alice,UntrustedAgent_12345...
    """
    agent_metadata = metadata or DEFAULT_AGENT_METADATA
    agent_id = f"{user_id},UntrustedAgent"

    try:
        agent_result = cast(
            dict[str, Any],
            nx.register_agent(
                agent_id=agent_id,
                name="UntrustedAgent",
                description="Untrusted agent with API key - zero permissions by default, read-only access granted explicitly",
                generate_api_key=True,  # Has its own API key
                inherit_permissions=False,  # Zero permissions by default
                metadata=agent_metadata,
                context=context,
            ),
        )
        print(
            f"  ✓ Created agent 'UntrustedAgent' (with API key, zero permissions) at {agent_result.get('config_path', 'N/A')}"
        )
        return agent_result
    except Exception as e:
        print(f"  ✗ Failed to create UntrustedAgent agent: {e}")
        return None


def create_standard_agents(
    nx: Any, user_id: str, context: Any, metadata: dict[str, Any] | None = None
) -> dict[str, dict[str, Any] | None]:
    """
    Create both standard agent types (ImpersonatedUser and UntrustedAgent).

    Convenience function to create both agents with a single call.

    Args:
        nx: NexusFS instance
        user_id: User ID to create agents for
        context: Operation context for the user
        metadata: Optional agent metadata (uses DEFAULT_AGENT_METADATA if not provided)

    Returns:
        Dictionary with 'impersonated' and 'untrusted' keys containing results

    Examples:
        >>> results = create_standard_agents(nx, "alice", alice_context)
        >>> if results['impersonated']:
        ...     print("Digital twin created successfully")
        >>> if results['untrusted']:
        ...     print(f"API key: {results['untrusted'].get('api_key')}")
    """
    return {
        "impersonated": create_impersonated_user_agent(nx, user_id, context, metadata),
        "untrusted": create_untrusted_agent(nx, user_id, context, metadata),
    }


def grant_agent_resource_access(
    nx: Any,
    user_id: str,
    tenant_id: str,
    resource_types: list[str],
    agent_name: str = "UntrustedAgent",
) -> int:
    """
    Grant viewer (read-only) permissions to agent for specified resource types.

    Args:
        nx: NexusFS instance
        user_id: User ID who owns the resources
        tenant_id: Tenant ID
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
    user_base_path = f"/tenant:{tenant_id}/user:{user_id}"
    granted_count = 0

    for resource_type in resource_types:
        folder_path = f"{user_base_path}/{resource_type}"
        try:
            nx.rebac_create(
                subject=("agent", agent_id),
                relation="viewer",  # Read-only access
                object=("file", folder_path),
                tenant_id=tenant_id,
            )
            print(f"  ✓ Granted viewer permission on {folder_path} to {agent_name}")
            granted_count += 1
        except Exception as e:
            print(f"  ✗ Failed to grant permission on {folder_path}: {e}")

    return granted_count
