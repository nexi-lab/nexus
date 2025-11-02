"""Agent registration and management (v0.5.0).

Provides auth-agnostic agent registration. Agents inherit permissions from
their owner (user) and can optionally have their own API keys.

Key concepts:
- Agent = identity owned by a user
- User-authenticated agent: Uses user's credentials + X-Agent-ID header
- Agent-authenticated: Has own API key (optional)
- No agent_type field: Lifecycle managed via API key TTL

See: docs/design/AGENT_IDENTITY_AND_SESSIONS.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.core.entity_registry import EntityRegistry


def register_agent(
    user_id: str,
    agent_id: str,
    name: str | None = None,
    tenant_id: str | None = None,
    metadata: dict | None = None,
    entity_registry: EntityRegistry | None = None,
) -> dict:
    """Register an agent (auth-agnostic).

    Creates entity registry entry for agent→user relationship.
    Does NOT create API keys or assume any auth mechanism.

    Most agents should just use this function and authenticate via
    user's credentials + X-Agent-ID header.

    Args:
        user_id: Owner of the agent
        agent_id: Unique agent identifier (e.g., "agent_data_analyst")
        name: Human-readable name (e.g., "Data Analyst")
        tenant_id: Organization ID
        metadata: Additional metadata
        entity_registry: Registry for relationships

    Returns:
        Agent info dict

    Examples:
        >>> # Register agent (no API key)
        >>> agent = register_agent(
        ...     user_id="alice",
        ...     agent_id="agent_data_analyst",
        ...     name="Data Analyst"
        ... )
        >>> # Agent can now be used by alice
        >>> # Authorization: Bearer <alice's_token>
        >>> # X-Agent-ID: agent_data_analyst
    """
    # Validate inputs
    if not user_id:
        raise ValueError("user_id is required")
    if not agent_id:
        raise ValueError("agent_id is required")

    # Create entity registry entry
    if entity_registry:
        # Prepare entity metadata with name and description
        entity_metadata_dict = {}
        if name:
            entity_metadata_dict["name"] = name
        # Store description from metadata if provided
        if metadata and "description" in metadata:
            entity_metadata_dict["description"] = metadata["description"]

        entity_registry.register_entity(
            entity_type="agent",
            entity_id=agent_id,
            parent_type="user",
            parent_id=user_id,
            entity_metadata=entity_metadata_dict if entity_metadata_dict else None,
        )

    return {
        "agent_id": agent_id,
        "user_id": user_id,
        "name": name,
        "tenant_id": tenant_id,
        "metadata": metadata or {},
        "created_at": datetime.now(UTC).isoformat(),
    }


def create_agent_with_api_key(
    session: Session,
    user_id: str,
    agent_id: str,
    name: str,
    expires_at: datetime | None = None,
    entity_registry: EntityRegistry | None = None,
    **kwargs: Any,
) -> tuple[dict, str]:
    """Convenience: Register agent + create API key.

    ONLY use if you want agent to authenticate independently.
    Most agents should use register_agent() and user's auth.

    Args:
        session: Database session
        user_id: Owner
        agent_id: Agent identifier
        name: Human-readable name
        expires_at: Optional TTL for API key (None = permanent)
        entity_registry: Registry for relationships
        **kwargs: Additional args for register_agent()

    Returns:
        (agent_info, raw_api_key)

    Examples:
        >>> # Create agent with temporary API key (1 hour)
        >>> agent, key = create_agent_with_api_key(
        ...     session,
        ...     user_id="alice",
        ...     agent_id="agent_task",
        ...     name="Task Agent",
        ...     expires_at=datetime.now(UTC) + timedelta(hours=1)
        ... )
        >>> # Agent authenticates with its own key
        >>> # Authorization: Bearer <agent_key>
    """
    # 1. Register agent
    agent = register_agent(user_id, agent_id, name, entity_registry=entity_registry, **kwargs)

    # 2. Create API key (provider-specific)
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth

    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
        session,
        user_id=user_id,
        name=name,
        subject_type="agent",
        subject_id=agent_id,
        expires_at=expires_at,
    )

    return agent, raw_key


def unregister_agent(agent_id: str, entity_registry: EntityRegistry | None = None) -> bool:
    """Unregister an agent.

    Removes entity registry entry. Does NOT revoke API keys - do that separately.

    Args:
        agent_id: Agent identifier
        entity_registry: Registry for relationships

    Returns:
        True if unregistered, False if not found
    """
    if entity_registry:
        return entity_registry.delete_entity("agent", agent_id)
    return False


def validate_agent_ownership(agent_id: str, user_id: str, entity_registry: EntityRegistry) -> bool:
    """Validate that agent belongs to user.

    Used in request handlers to verify agent ownership.

    Args:
        agent_id: Agent identifier
        user_id: Expected owner
        entity_registry: Registry for relationships

    Returns:
        True if agent is owned by user, False otherwise

    Examples:
        >>> # In request handler
        >>> agent_id = request.headers.get("X-Agent-ID")
        >>> if agent_id and not validate_agent_ownership(agent_id, user.user_id, registry):
        ...     raise PermissionError(f"Agent {agent_id} not owned by {user.user_id}")
    """
    agent = entity_registry.get_entity("agent", agent_id)
    if not agent:
        return False
    return agent.parent_type == "user" and agent.parent_id == user_id
