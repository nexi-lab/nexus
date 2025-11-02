"""Entity Registry for Identity-Based Memory System (v0.4.0).

Lightweight registry for ID disambiguation and relationship tracking.
Enables order-neutral virtual paths for memories.
"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.storage.models import EntityRegistryModel

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker


class EntityRegistry:
    """Entity registry for managing identity relationships.

    v0.5.0: Refactored to use SessionFactory instead of holding a session.
    This improves resource management and prevents session lifecycle issues.
    """

    _session: "Session | None"
    _session_factory: "sessionmaker[Session] | None"

    def __init__(self, session_or_factory: "Session | sessionmaker[Session] | Engine"):
        """Initialize entity registry.

        Args:
            session_or_factory: Can be:
                - Session: Legacy support (will be deprecated)
                - sessionmaker: Session factory (recommended)
                - Engine: SQLAlchemy engine (will create sessionmaker)

        Examples:
            # Recommended: Pass SessionFactory
            >>> from sqlalchemy.orm import sessionmaker
            >>> SessionLocal = sessionmaker(bind=engine)
            >>> registry = EntityRegistry(SessionLocal)

            # Or pass Engine directly
            >>> registry = EntityRegistry(engine)

            # Legacy: Pass Session (for backward compatibility)
            >>> session = SessionLocal()
            >>> registry = EntityRegistry(session)
        """
        from sqlalchemy.engine import Engine
        from sqlalchemy.orm import Session, sessionmaker

        if isinstance(session_or_factory, Session):
            # Legacy mode: Hold the session (backward compatibility)
            self._session = session_or_factory
            self._session_factory = None
        elif isinstance(session_or_factory, Engine):
            # Engine provided: Create sessionmaker
            self._session = None
            self._session_factory = sessionmaker(bind=session_or_factory, expire_on_commit=False)
        else:
            # SessionFactory provided (recommended)
            self._session = None
            self._session_factory = session_or_factory

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        """Get a session (creates new if using factory, or uses held session).

        Yields:
            Session: Database session
        """
        if self._session:
            # Legacy mode: Use the held session, don't close it
            yield self._session
        else:
            # New mode: Create session from factory, close after use
            if self._session_factory is None:
                raise RuntimeError("No session or session factory configured")
            session = self._session_factory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        parent_type: str | None = None,
        parent_id: str | None = None,
        entity_metadata: dict | None = None,
    ) -> EntityRegistryModel:
        """Register an entity in the registry.

        Args:
            entity_type: Type of entity ('tenant', 'user', 'agent').
            entity_id: Unique identifier for the entity.
            parent_type: Type of parent entity (optional).
            parent_id: ID of parent entity (optional).
            entity_metadata: Additional metadata as dict (optional). Will be stored as JSON.
                            For agents: {'name': 'Display Name', 'description': 'Agent description'}

        Returns:
            EntityRegistryModel: The registered entity.

        Raises:
            ValueError: If entity_type is invalid or parent is inconsistent.
        """
        import logging

        logger = logging.getLogger(__name__)

        logger.warning(
            f"[ENTITY-REG] register_entity called: {entity_type}:{entity_id}, parent={parent_type}:{parent_id}"
        )

        # Check if entity already exists
        existing = self.get_entity(entity_type, entity_id)
        if existing:
            logger.warning(f"[ENTITY-REG] Entity already exists: {entity_type}:{entity_id}")
            return existing

        # Create new entity
        with self._get_session() as session:
            import json

            # Serialize metadata to JSON string if provided
            metadata_json = json.dumps(entity_metadata) if entity_metadata else None

            entity = EntityRegistryModel(
                entity_type=entity_type,
                entity_id=entity_id,
                parent_type=parent_type,
                parent_id=parent_id,
                entity_metadata=metadata_json,
                created_at=datetime.now(UTC),
            )

            # Validate before adding
            entity.validate()

            session.add(entity)
            session.commit()

            # Refresh to ensure we have the committed state
            session.refresh(entity)
            logger.warning(
                f"[ENTITY-REG] Entity registered successfully: {entity_type}:{entity_id}"
            )
            return entity

    def get_entity(self, entity_type: str, entity_id: str) -> EntityRegistryModel | None:
        """Get an entity from the registry.

        Args:
            entity_type: Type of entity.
            entity_id: Unique identifier.

        Returns:
            EntityRegistryModel or None if not found.
        """
        with self._get_session() as session:
            stmt = select(EntityRegistryModel).where(
                EntityRegistryModel.entity_type == entity_type,
                EntityRegistryModel.entity_id == entity_id,
            )
            result: EntityRegistryModel | None = session.execute(stmt).scalar_one_or_none()
            if result:
                session.expunge(result)  # Detach from session so it can be used outside
            return result

    def lookup_entity_by_id(self, entity_id: str) -> list[EntityRegistryModel]:
        """Look up entities by ID (may return multiple if ID is not unique across types).

        Args:
            entity_id: Entity identifier to look up.

        Returns:
            List of matching entities.
        """
        with self._get_session() as session:
            stmt = select(EntityRegistryModel).where(EntityRegistryModel.entity_id == entity_id)
            results = list(session.execute(stmt).scalars().all())
            # Detach all results from session
            for result in results:
                session.expunge(result)
            return results

    def get_entities_by_type(self, entity_type: str) -> list[EntityRegistryModel]:
        """Get all entities of a specific type.

        Args:
            entity_type: Type of entity.

        Returns:
            List of entities.
        """
        with self._get_session() as session:
            stmt = select(EntityRegistryModel).where(EntityRegistryModel.entity_type == entity_type)
            results = list(session.execute(stmt).scalars().all())
            # Detach all results from session
            for result in results:
                session.expunge(result)
            return results

    def get_parent(self, entity_type: str, entity_id: str) -> EntityRegistryModel | None:
        """Get the parent entity of a given entity.

        v0.5.0 ACE: Used for agent→user permission inheritance

        Args:
            entity_type: Type of entity (e.g., "agent")
            entity_id: ID of entity (e.g., "agent_data_analyst")

        Returns:
            Parent EntityRegistryModel or None if no parent

        Example:
            >>> # Get agent's owner (user)
            >>> parent = registry.get_parent("agent", "agent_data_analyst")
            >>> if parent and parent.parent_type == "user":
            ...     print(f"Agent owned by user: {parent.parent_id}")
        """
        import logging

        logger = logging.getLogger(__name__)

        logger.warning(
            f"[ENTITY-REG] get_parent called: entity_type={entity_type}, entity_id={entity_id}"
        )

        entity = self.get_entity(entity_type, entity_id)

        if not entity:
            logger.warning(f"[ENTITY-REG] Entity NOT found in database: {entity_type}:{entity_id}")
            return None

        logger.warning(
            f"[ENTITY-REG] Entity found: {entity.entity_type}:{entity.entity_id}, parent={entity.parent_type}:{entity.parent_id}"
        )

        if not entity.parent_type or not entity.parent_id:
            logger.warning("[ENTITY-REG] Entity has no parent")
            return None

        # Get the parent entity
        parent = self.get_entity(entity.parent_type, entity.parent_id)
        if parent:
            logger.warning(f"[ENTITY-REG] Parent found: {parent.entity_type}:{parent.entity_id}")
        else:
            logger.warning(
                f"[ENTITY-REG] Parent NOT found: {entity.parent_type}:{entity.parent_id}"
            )
        return parent

    def get_children(self, parent_type: str, parent_id: str) -> list[EntityRegistryModel]:
        """Get all child entities of a parent.

        Args:
            parent_type: Type of parent entity.
            parent_id: ID of parent entity.

        Returns:
            List of child entities.
        """
        with self._get_session() as session:
            stmt = select(EntityRegistryModel).where(
                EntityRegistryModel.parent_type == parent_type,
                EntityRegistryModel.parent_id == parent_id,
            )
            results = list(session.execute(stmt).scalars().all())
            # Detach all results from session
            for result in results:
                session.expunge(result)
            return results

    def delete_entity(self, entity_type: str, entity_id: str, cascade: bool = True) -> bool:
        """Delete an entity from the registry.

        Args:
            entity_type: Type of entity.
            entity_id: Unique identifier.
            cascade: If True, recursively delete child entities (default: True).
                     When deleting a user, all their owned agents are also deleted.
                     When deleting a tenant, all users and agents are also deleted.

        Returns:
            True if deleted, False if not found.

        Examples:
            >>> # Delete user and all their agents (cascade=True by default)
            >>> registry.delete_entity("user", "alice")
            True

            >>> # Delete only the user, leave agents orphaned (not recommended)
            >>> registry.delete_entity("user", "alice", cascade=False)
            True
        """
        # Check if entity exists
        entity = self.get_entity(entity_type, entity_id)
        if not entity:
            return False

        # Cascade delete: recursively delete all child entities first
        if cascade:
            children = self.get_children(entity_type, entity_id)
            for child in children:
                # Recursively delete child (with cascade)
                self.delete_entity(child.entity_type, child.entity_id, cascade=True)

        # Delete the entity itself using a fresh query in the session context
        with self._get_session() as session:
            # Query the entity within this session (don't reuse detached entity)
            stmt = select(EntityRegistryModel).where(
                EntityRegistryModel.entity_type == entity_type,
                EntityRegistryModel.entity_id == entity_id,
            )
            entity_to_delete = session.execute(stmt).scalar_one_or_none()

            if entity_to_delete:
                session.delete(entity_to_delete)
                session.commit()

        return True

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
