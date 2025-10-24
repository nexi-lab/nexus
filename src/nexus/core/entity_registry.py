"""Entity Registry for Identity-Based Memory System (v0.4.0).

Lightweight registry for ID disambiguation and relationship tracking.
Enables order-neutral virtual paths for memories.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.storage.models import EntityRegistryModel


class EntityRegistry:
    """Entity registry for managing identity relationships."""

    def __init__(self, session: Session):
        """Initialize entity registry.

        Args:
            session: SQLAlchemy database session.
        """
        self.session = session

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        parent_type: str | None = None,
        parent_id: str | None = None,
    ) -> EntityRegistryModel:
        """Register an entity in the registry.

        Args:
            entity_type: Type of entity ('tenant', 'user', 'agent').
            entity_id: Unique identifier for the entity.
            parent_type: Type of parent entity (optional).
            parent_id: ID of parent entity (optional).

        Returns:
            EntityRegistryModel: The registered entity.

        Raises:
            ValueError: If entity_type is invalid or parent is inconsistent.
        """
        # Check if entity already exists
        existing = self.get_entity(entity_type, entity_id)
        if existing:
            return existing

        # Create new entity
        entity = EntityRegistryModel(
            entity_type=entity_type,
            entity_id=entity_id,
            parent_type=parent_type,
            parent_id=parent_id,
            created_at=datetime.now(UTC),
        )

        # Validate before adding
        entity.validate()

        self.session.add(entity)
        self.session.commit()
        return entity

    def get_entity(self, entity_type: str, entity_id: str) -> EntityRegistryModel | None:
        """Get an entity from the registry.

        Args:
            entity_type: Type of entity.
            entity_id: Unique identifier.

        Returns:
            EntityRegistryModel or None if not found.
        """
        stmt = select(EntityRegistryModel).where(
            EntityRegistryModel.entity_type == entity_type,
            EntityRegistryModel.entity_id == entity_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def lookup_entity_by_id(self, entity_id: str) -> list[EntityRegistryModel]:
        """Look up entities by ID (may return multiple if ID is not unique across types).

        Args:
            entity_id: Entity identifier to look up.

        Returns:
            List of matching entities.
        """
        stmt = select(EntityRegistryModel).where(EntityRegistryModel.entity_id == entity_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_entities_by_type(self, entity_type: str) -> list[EntityRegistryModel]:
        """Get all entities of a specific type.

        Args:
            entity_type: Type of entity.

        Returns:
            List of entities.
        """
        stmt = select(EntityRegistryModel).where(EntityRegistryModel.entity_type == entity_type)
        return list(self.session.execute(stmt).scalars().all())

    def get_children(self, parent_type: str, parent_id: str) -> list[EntityRegistryModel]:
        """Get all child entities of a parent.

        Args:
            parent_type: Type of parent entity.
            parent_id: ID of parent entity.

        Returns:
            List of child entities.
        """
        stmt = select(EntityRegistryModel).where(
            EntityRegistryModel.parent_type == parent_type,
            EntityRegistryModel.parent_id == parent_id,
        )
        return list(self.session.execute(stmt).scalars().all())

    def delete_entity(self, entity_type: str, entity_id: str) -> bool:
        """Delete an entity from the registry.

        Args:
            entity_type: Type of entity.
            entity_id: Unique identifier.

        Returns:
            True if deleted, False if not found.
        """
        entity = self.get_entity(entity_type, entity_id)
        if entity:
            self.session.delete(entity)
            self.session.commit()
            return True
        return False

    def auto_register_from_config(self, config: dict[str, Any]) -> None:
        """Auto-register entities from Nexus config.

        Args:
            config: Nexus configuration dictionary containing tenant_id, user_id, agent_id.
        """
        tenant_id = config.get("tenant_id")
        user_id = config.get("user_id")
        agent_id = config.get("agent_id")

        # Register tenant (top-level)
        if tenant_id:
            self.register_entity("tenant", tenant_id)

        # Register user (child of tenant)
        if user_id:
            self.register_entity(
                "user", user_id, parent_type="tenant" if tenant_id else None, parent_id=tenant_id
            )

        # Register agent (child of user)
        if agent_id:
            self.register_entity(
                "agent", agent_id, parent_type="user" if user_id else None, parent_id=user_id
            )

    def extract_ids_from_path_parts(self, parts: list[str]) -> dict[str, str]:
        """Extract entity IDs from path parts using registry lookup.

        This enables order-neutral path resolution: /workspace/alice/agent1
        and /workspace/agent1/alice resolve to the same IDs.

        Args:
            parts: List of path components.

        Returns:
            Dictionary mapping entity type keys to IDs (e.g., {'user_id': 'alice', 'agent_id': 'agent1'}).
        """
        ids: dict[str, str] = {}

        for part in parts:
            # Skip empty parts and known namespace prefixes
            if not part or part in [
                "workspace",
                "shared",
                "memory",
                "objs",
                "by-user",
                "by-agent",
                "by-tenant",
            ]:
                continue

            # Look up in registry
            entities = self.lookup_entity_by_id(part)

            for entity in entities:
                # Map entity_type to ID key
                id_key = f"{entity.entity_type}_id"
                if id_key not in ids:  # Don't overwrite if already set
                    ids[id_key] = entity.entity_id

        return ids
