"""Agent Service - Stateless business logic for agent operations.

This service implements the Gateway pattern for agent management:
- config.yaml is the single source of truth for agent metadata
- EntityRegistry stores only agent→user relationship for permission inheritance
- ReBAC manages agent capabilities (skills, resources)

## API Summary

CRUD:
- register: Create a new agent with config.yaml
- get: Get agent details from config.yaml
- list: List agents by globbing config.yaml files
- update: Update agent configuration
- delete: Delete agent and cleanup resources

Capabilities:
- get_context: Load full runtime context for agent execution

## RFC Reference
See docs/rfc/001-agent-service-refactor.md for full design.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.core.entity_registry import EntityRegistry
    from nexus.services.gateway import NexusFSGateway

logger = logging.getLogger(__name__)


# =============================================================================
# Dataclasses
# =============================================================================


@dataclass
class AgentRuntime:
    """Agent runtime/platform configuration."""

    platform: str = "langgraph"  # langgraph, openai-assistants, anthropic, bedrock, custom
    endpoint_url: str = "http://localhost:2024"
    assistant_id: str = "agent"
    auth_type: str = "none"  # api_key, oauth, none
    auth_env_var: str | None = None
    options: dict = field(default_factory=dict)


@dataclass
class AgentCapabilities:
    """Agent capability declarations."""

    skills: list[dict] = field(default_factory=list)  # [{"name": "...", "relation": "viewer"}]
    resources: list[dict] = field(default_factory=list)  # [{"path": "...", "relation": "viewer"}]


@dataclass
class AgentContext:
    """Runtime context for an agent."""

    agent_id: str
    name: str
    user_id: str
    tenant_id: str
    description: str | None
    role_prompt: str | None
    runtime: AgentRuntime
    capabilities: AgentCapabilities
    skills: list[dict] = field(default_factory=list)  # Loaded SkillPromptContext dicts
    api_key: str | None = None

    def build_system_prompt(self) -> str:
        """Build complete system prompt with role and skill descriptions."""
        parts = []

        if self.role_prompt:
            parts.append(self.role_prompt)

        if self.skills:
            parts.append("\n## Available Skills\n")
            for skill in self.skills:
                name = skill.get("name", "Unknown")
                description = skill.get("description", "")
                parts.append(f"### {name}\n{description}\n")

        return "\n".join(parts)


# =============================================================================
# AgentService
# =============================================================================


class AgentService:
    """Agent management service.

    Uses config.yaml as single source of truth for agent data.
    EntityRegistry stores only agent→user relationship for permission inheritance.

    Example:
        ```python
        from nexus.services.gateway import NexusFSGateway
        from nexus.core.entity_registry import EntityRegistry

        gateway = NexusFSGateway(fs)
        entity_registry = EntityRegistry(session_factory)
        agent_service = AgentService(gateway, entity_registry, session_factory)

        # Register a new agent
        agent = agent_service.register(
            agent_id="alice,DataAnalyst",
            name="Data Analyst",
            user_id="alice",
            tenant_id="default",
            role_prompt="You are a data analyst...",
            capabilities={
                "skills": [{"name": "data-viz", "relation": "viewer"}],
            },
        )

        # Get agent runtime context
        ctx = agent_service.get_context("alice,DataAnalyst", "alice", "default")
        system_prompt = ctx.build_system_prompt()
        ```
    """

    def __init__(
        self,
        gateway: NexusFSGateway,
        entity_registry: EntityRegistry,
        session_factory: Callable[[], Session],
    ) -> None:
        """Initialize agent service.

        Args:
            gateway: NexusFSGateway for filesystem and ReBAC operations
            entity_registry: EntityRegistry for agent→user relationships
            session_factory: SQLAlchemy session factory for API key operations
        """
        self._gw = gateway
        self._entity_registry = entity_registry
        self._session_factory = session_factory

        logger.info("[AgentService] Initialized")

    # =========================================================================
    # Path Helpers
    # =========================================================================

    def _get_agent_dir(self, agent_id: str, user_id: str, tenant_id: str) -> str:
        """Get agent directory path."""
        agent_name = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        return f"/tenant:{tenant_id}/user:{user_id}/agent/{agent_name}"

    def _get_config_path(self, agent_id: str, user_id: str, tenant_id: str) -> str:
        """Get agent config.yaml path."""
        return f"{self._get_agent_dir(agent_id, user_id, tenant_id)}/config.yaml"

    # =========================================================================
    # Config I/O
    # =========================================================================

    def _read_config(self, agent_id: str, user_id: str, tenant_id: str) -> dict | None:
        """Read and parse agent config.yaml.

        Returns:
            Parsed config dict, or None if not found
        """
        config_path = self._get_config_path(agent_id, user_id, tenant_id)
        try:
            content = self._gw.read(config_path)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            return yaml.safe_load(content)
        except Exception as e:
            logger.debug(f"Failed to read agent config {config_path}: {e}")
            return None

    def _write_config(self, agent_id: str, user_id: str, tenant_id: str, config: dict) -> None:
        """Write agent config.yaml."""
        config_path = self._get_config_path(agent_id, user_id, tenant_id)
        content = yaml.dump(config, default_flow_style=False, sort_keys=False)
        self._gw.write(config_path, content)

    def _build_config(
        self,
        agent_id: str,
        name: str,
        user_id: str,
        description: str | None = None,
        role_prompt: str | None = None,
        runtime: dict | None = None,
        capabilities: dict | None = None,
        api_key: str | None = None,
        created_at: str | None = None,
    ) -> dict:
        """Build agent config.yaml structure."""
        config: dict[str, Any] = {
            "agent_id": agent_id,
            "name": name,
            "user_id": user_id,
            "description": description,
            "created_at": created_at or datetime.now(UTC).isoformat(),
        }

        if runtime:
            config["runtime"] = runtime

        if role_prompt:
            config["role_prompt"] = role_prompt

        if capabilities:
            config["capabilities"] = capabilities

        if api_key:
            config["api_key"] = api_key

        return config

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def register(
        self,
        agent_id: str,
        name: str,
        user_id: str,
        tenant_id: str,
        description: str | None = None,
        role_prompt: str | None = None,
        runtime: dict | None = None,
        capabilities: dict | None = None,
        generate_api_key: bool = False,
    ) -> dict:
        """Register a new agent.

        Creates:
        1. Agent directory and config.yaml
        2. EntityRegistry entry (relationship only)
        3. ReBAC permissions for agent directory
        4. ReBAC permissions for declared capabilities
        5. Optional API key

        Args:
            agent_id: Unique agent identifier (format: user_id,agent_name)
            name: Human-readable name
            user_id: Owner user ID
            tenant_id: Tenant ID
            description: Optional description
            role_prompt: Optional system prompt
            runtime: Optional runtime configuration
            capabilities: Optional capabilities (skills, resources)
            generate_api_key: If True, create API key for agent

        Returns:
            Agent info dict

        Raises:
            ValueError: If agent already exists
        """
        agent_dir = self._get_agent_dir(agent_id, user_id, tenant_id)
        config_path = self._get_config_path(agent_id, user_id, tenant_id)

        # Check if agent already exists
        if self._gw.exists(config_path):
            raise ValueError(
                f"Agent already exists at {config_path}. "
                "Delete the agent first if you want to recreate it."
            )

        # 1. Register in EntityRegistry (relationship only, no metadata)
        self._entity_registry.register_entity(
            entity_type="agent",
            entity_id=agent_id,
            parent_type="user",
            parent_id=user_id,
            entity_metadata=None,  # No metadata - config.yaml is source of truth
        )
        logger.info(f"Registered agent entity: {agent_id} -> user:{user_id}")

        # 2. Create agent directory
        self._gw.mkdir(agent_dir, parents=True, exist_ok=True)

        # 3. Build and write config.yaml
        created_at = datetime.now(UTC).isoformat()
        config = self._build_config(
            agent_id=agent_id,
            name=name,
            user_id=user_id,
            description=description,
            role_prompt=role_prompt,
            runtime=runtime,
            capabilities=capabilities,
            created_at=created_at,
        )
        self._write_config(agent_id, user_id, tenant_id, config)
        logger.info(f"Created agent config: {config_path}")

        # 4. Grant ReBAC permissions
        self._grant_agent_permissions(agent_id, user_id, tenant_id, agent_dir)

        # 5. Sync capabilities to ReBAC
        if capabilities:
            self._sync_capabilities(agent_id, user_id, tenant_id, capabilities)

        # 6. Optional: Generate API key
        api_key = None
        if generate_api_key:
            api_key = self._create_api_key(agent_id, user_id, tenant_id)
            # Update config with API key
            config["api_key"] = api_key
            self._write_config(agent_id, user_id, tenant_id, config)

        return {
            "agent_id": agent_id,
            "name": name,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "description": description,
            "config_path": config_path,
            "has_api_key": api_key is not None,
            "api_key": api_key,
            "created_at": created_at,
        }

    def get(self, agent_id: str, user_id: str, tenant_id: str) -> dict | None:
        """Get agent details from config.yaml.

        Args:
            agent_id: Agent identifier
            user_id: Owner user ID
            tenant_id: Tenant ID

        Returns:
            Agent info dict, or None if not found
        """
        config = self._read_config(agent_id, user_id, tenant_id)
        if not config:
            return None

        config_path = self._get_config_path(agent_id, user_id, tenant_id)

        # Check if agent has API key in database
        has_api_key = self._check_has_api_key(agent_id)

        return {
            "agent_id": config.get("agent_id", agent_id),
            "name": config.get("name", agent_id),
            "user_id": config.get("user_id", user_id),
            "tenant_id": tenant_id,
            "description": config.get("description"),
            "role_prompt": config.get("role_prompt"),
            "runtime": config.get("runtime"),
            "capabilities": config.get("capabilities"),
            "config_path": config_path,
            "has_api_key": has_api_key,
            "created_at": config.get("created_at"),
        }

    def list(
        self,
        user_id: str | None = None,
        tenant_id: str = "default",
    ) -> list[dict]:
        """List agents by globbing config.yaml files.

        Args:
            user_id: If provided, only list agents for this user
            tenant_id: Tenant ID

        Returns:
            List of agent info dicts
        """
        # Use metadata list to find agent configs
        agents = []
        try:
            # Glob through metadata
            prefix = f"/tenant:{tenant_id}/"
            for meta in self._gw.metadata_list(prefix, recursive=True):
                path = meta.path
                if "/agent/" in path and path.endswith("/config.yaml"):
                    # Filter by user_id if specified
                    if user_id and f"/user:{user_id}/" not in path:
                        continue

                    # Extract agent info from path
                    try:
                        # Path format: /tenant:{tid}/user:{uid}/agent/{name}/config.yaml
                        parts = path.split("/")
                        agent_name = parts[-2]  # Second to last is agent name
                        owner_id = None
                        for part in parts:
                            if part.startswith("user:"):
                                owner_id = part[5:]
                                break

                        if owner_id:
                            agent_id = f"{owner_id},{agent_name}"
                            agent_info = self.get(agent_id, owner_id, tenant_id)
                            if agent_info:
                                agents.append(agent_info)
                    except Exception as e:
                        logger.debug(f"Failed to parse agent from {path}: {e}")
        except Exception as e:
            logger.warning(f"Failed to list agents: {e}")

        return agents

    def update(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
        name: str | None = None,
        description: str | None = None,
        role_prompt: str | None = None,
        runtime: dict | None = None,
        capabilities: dict | None = None,
    ) -> dict:
        """Update agent configuration.

        Args:
            agent_id: Agent identifier
            user_id: Owner user ID
            tenant_id: Tenant ID
            name: New name (optional)
            description: New description (optional)
            role_prompt: New role prompt (optional)
            runtime: New runtime config (optional)
            capabilities: New capabilities (optional)

        Returns:
            Updated agent info dict

        Raises:
            ValueError: If agent not found
        """
        config = self._read_config(agent_id, user_id, tenant_id)
        if not config:
            raise ValueError(f"Agent not found: {agent_id}")

        old_capabilities = config.get("capabilities", {})

        # Update fields
        if name is not None:
            config["name"] = name
        if description is not None:
            config["description"] = description
        if role_prompt is not None:
            config["role_prompt"] = role_prompt
        if runtime is not None:
            config["runtime"] = runtime
        if capabilities is not None:
            config["capabilities"] = capabilities

        # Write updated config
        self._write_config(agent_id, user_id, tenant_id, config)

        # Update capabilities in ReBAC if changed
        if capabilities is not None:
            self._update_capabilities(agent_id, user_id, tenant_id, old_capabilities, capabilities)

        return self.get(agent_id, user_id, tenant_id) or {}

    def delete(self, agent_id: str, user_id: str, tenant_id: str) -> bool:
        """Delete agent and cleanup resources.

        Cleans up:
        1. Agent directory and config.yaml
        2. EntityRegistry entry
        3. ReBAC permissions
        4. API keys

        Args:
            agent_id: Agent identifier
            user_id: Owner user ID
            tenant_id: Tenant ID

        Returns:
            True if deleted, False if not found
        """
        agent_dir = self._get_agent_dir(agent_id, user_id, tenant_id)

        # Check if agent exists
        if not self._gw.exists(agent_dir):
            return False

        # 1. Revoke all ReBAC tuples for agent
        self._gw.rebac_delete_object_tuples(
            object=("file", agent_dir),
            tenant_id=tenant_id,
        )

        # 2. Revoke capability permissions
        config = self._read_config(agent_id, user_id, tenant_id)
        if config and config.get("capabilities"):
            self._revoke_capabilities(agent_id, user_id, tenant_id, config["capabilities"])

        # 3. Delete agent directory (via NexusFS)
        try:
            self._gw._fs.rmdir(agent_dir, recursive=True, is_admin=True)
        except Exception as e:
            logger.warning(f"Failed to delete agent directory {agent_dir}: {e}")

        # 4. Revoke API keys
        self._revoke_api_keys(agent_id)

        # 5. Delete from EntityRegistry
        self._entity_registry.delete_entity("agent", agent_id)

        logger.info(f"Deleted agent: {agent_id}")
        return True

    # =========================================================================
    # Runtime Context
    # =========================================================================

    def get_context(self, agent_id: str, user_id: str, tenant_id: str) -> AgentContext | None:
        """Load agent's full runtime context.

        Returns everything an agent needs to start:
        - Runtime platform configuration
        - Role prompt for system message
        - Loaded skill contexts
        - Resource access list

        Args:
            agent_id: Agent identifier
            user_id: Owner user ID
            tenant_id: Tenant ID

        Returns:
            AgentContext with full runtime info, or None if not found
        """
        config = self._read_config(agent_id, user_id, tenant_id)
        if not config:
            return None

        # Parse runtime configuration
        runtime_config = config.get("runtime", {})
        runtime = AgentRuntime(
            platform=runtime_config.get("platform", "langgraph"),
            endpoint_url=runtime_config.get("endpoint_url", "http://localhost:2024"),
            assistant_id=runtime_config.get("assistant_id", "agent"),
            auth_type=runtime_config.get("auth", {}).get("type", "none"),
            auth_env_var=runtime_config.get("auth", {}).get("env_var"),
            options=runtime_config.get("options", {}),
        )

        # Parse capabilities
        caps_config = config.get("capabilities", {})
        capabilities = AgentCapabilities(
            skills=caps_config.get("skills", []),
            resources=caps_config.get("resources", []),
        )

        # Load skill contexts (simplified - just return declared skills for now)
        # Full SkillService integration would load actual skill content
        skill_contexts = []
        for skill in capabilities.skills:
            skill_contexts.append(
                {
                    "name": skill.get("name", ""),
                    "relation": skill.get("relation", "viewer"),
                }
            )

        return AgentContext(
            agent_id=agent_id,
            name=config.get("name", agent_id),
            user_id=user_id,
            tenant_id=tenant_id,
            description=config.get("description"),
            role_prompt=config.get("role_prompt"),
            runtime=runtime,
            capabilities=capabilities,
            skills=skill_contexts,
            api_key=config.get("api_key"),
        )

    # =========================================================================
    # Permission Helpers
    # =========================================================================

    def _grant_agent_permissions(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
        agent_dir: str,
    ) -> None:
        """Grant ReBAC permissions for agent directory."""
        # Grant direct_owner to the agent itself
        self._gw.rebac_create(
            subject=("agent", agent_id),
            relation="direct_owner",
            object=("file", agent_dir),
            tenant_id=tenant_id,
        )

        # Grant direct_owner to the user
        self._gw.rebac_create(
            subject=("user", user_id),
            relation="direct_owner",
            object=("file", agent_dir),
            tenant_id=tenant_id,
        )

        # Grant viewer to agent for its own directory
        self._gw.rebac_create(
            subject=("agent", agent_id),
            relation="viewer",
            object=("file", agent_dir),
            tenant_id=tenant_id,
        )

        logger.debug(f"Granted permissions for agent {agent_id} on {agent_dir}")

    def _sync_capabilities(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
        capabilities: dict,
    ) -> None:
        """Sync declared capabilities to ReBAC tuples."""
        user_base = f"/tenant:{tenant_id}/user:{user_id}"

        # Sync skill access
        for skill in capabilities.get("skills", []):
            skill_name = skill.get("name")
            if not skill_name:
                continue
            skill_path = f"{user_base}/skill/{skill_name}"
            self._gw.rebac_create(
                subject=("agent", agent_id),
                relation=skill.get("relation", "viewer"),
                object=("file", skill_path),
                tenant_id=tenant_id,
            )
            logger.debug(f"Granted {skill.get('relation', 'viewer')} on {skill_path} to {agent_id}")

        # Sync resource access
        for resource in capabilities.get("resources", []):
            resource_path_rel = resource.get("path")
            if not resource_path_rel:
                continue
            resource_path = f"{user_base}{resource_path_rel}"
            self._gw.rebac_create(
                subject=("agent", agent_id),
                relation=resource.get("relation", "viewer"),
                object=("file", resource_path),
                tenant_id=tenant_id,
            )
            logger.debug(
                f"Granted {resource.get('relation', 'viewer')} on {resource_path} to {agent_id}"
            )

    def _revoke_capabilities(
        self,
        _agent_id: str,
        user_id: str,
        tenant_id: str,
        capabilities: dict,
    ) -> None:
        """Revoke capability ReBAC tuples for agent."""
        user_base = f"/tenant:{tenant_id}/user:{user_id}"

        # Revoke skill access
        for skill in capabilities.get("skills", []):
            skill_name = skill.get("name")
            if not skill_name:
                continue
            skill_path = f"{user_base}/skill/{skill_name}"
            self._gw.rebac_delete_object_tuples(
                object=("file", skill_path),
                tenant_id=tenant_id,
            )

        # Revoke resource access
        for resource in capabilities.get("resources", []):
            resource_path_rel = resource.get("path")
            if not resource_path_rel:
                continue
            resource_path = f"{user_base}{resource_path_rel}"
            self._gw.rebac_delete_object_tuples(
                object=("file", resource_path),
                tenant_id=tenant_id,
            )

    def _update_capabilities(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
        old_capabilities: dict,
        new_capabilities: dict,
    ) -> None:
        """Update capabilities: add new, remove old."""
        # Compare skills
        old_skills = {s.get("name") for s in old_capabilities.get("skills", []) if s.get("name")}
        new_skills = {s.get("name") for s in new_capabilities.get("skills", []) if s.get("name")}

        user_base = f"/tenant:{tenant_id}/user:{user_id}"

        # Revoke removed skills
        for skill_name in old_skills - new_skills:
            skill_path = f"{user_base}/skill/{skill_name}"
            self._gw.rebac_delete_object_tuples(
                object=("file", skill_path),
                tenant_id=tenant_id,
            )

        # Grant new skills
        for skill in new_capabilities.get("skills", []):
            if skill.get("name") in new_skills - old_skills:
                skill_path = f"{user_base}/skill/{skill['name']}"
                self._gw.rebac_create(
                    subject=("agent", agent_id),
                    relation=skill.get("relation", "viewer"),
                    object=("file", skill_path),
                    tenant_id=tenant_id,
                )

        # Same for resources
        old_resources = {
            r.get("path") for r in old_capabilities.get("resources", []) if r.get("path")
        }
        new_resources = {
            r.get("path") for r in new_capabilities.get("resources", []) if r.get("path")
        }

        for resource_path_rel in old_resources - new_resources:
            resource_path = f"{user_base}{resource_path_rel}"
            self._gw.rebac_delete_object_tuples(
                object=("file", resource_path),
                tenant_id=tenant_id,
            )

        for resource in new_capabilities.get("resources", []):
            if resource.get("path") in new_resources - old_resources:
                resource_path = f"{user_base}{resource['path']}"
                self._gw.rebac_create(
                    subject=("agent", agent_id),
                    relation=resource.get("relation", "viewer"),
                    object=("file", resource_path),
                    tenant_id=tenant_id,
                )

    # =========================================================================
    # API Key Helpers
    # =========================================================================

    def _create_api_key(self, agent_id: str, user_id: str, tenant_id: str) -> str:
        """Generate API key for agent."""
        from datetime import timedelta

        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        session = self._session_factory()
        try:
            # Default expiration: 365 days
            expires_at = datetime.now(UTC) + timedelta(days=365)

            _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id=user_id,
                name=agent_id,
                subject_type="agent",
                subject_id=agent_id,
                tenant_id=tenant_id,
                expires_at=expires_at,
            )
            session.commit()
            return raw_key
        finally:
            session.close()

    def _check_has_api_key(self, agent_id: str) -> bool:
        """Check if agent has an active API key."""
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        session = self._session_factory()
        try:
            stmt = select(APIKeyModel).where(
                APIKeyModel.subject_type == "agent",
                APIKeyModel.subject_id == agent_id,
                APIKeyModel.revoked == 0,
            )
            return session.scalar(stmt) is not None
        finally:
            session.close()

    def _revoke_api_keys(self, agent_id: str) -> None:
        """Revoke all API keys for agent."""
        from sqlalchemy import update

        from nexus.storage.models import APIKeyModel

        session = self._session_factory()
        try:
            stmt = (
                update(APIKeyModel)
                .where(
                    APIKeyModel.subject_type == "agent",
                    APIKeyModel.subject_id == agent_id,
                )
                .values(revoked=1)
            )
            session.execute(stmt)
            session.commit()
        finally:
            session.close()
