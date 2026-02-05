"""Memory View Router for Order-Neutral Paths (v0.4.0).

Resolves virtual paths to canonical memory IDs regardless of path order.
Enables multiple virtual path views for the same memory.

Includes temporal query operators (Issue #1023) for time-based filtering.
Includes version tracking (#1184) for memory audit trails.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from nexus.core.entity_registry import EntityRegistry
from nexus.storage.models import MemoryModel, VersionHistoryModel


class MemoryViewRouter:
    """Router for resolving virtual paths to canonical memory IDs."""

    def __init__(self, session: Session, entity_registry: EntityRegistry | None = None):
        """Initialize memory view router.

        Args:
            session: SQLAlchemy database session.
            entity_registry: Entity registry instance (creates new if None).
        """
        self.session = session
        self.entity_registry = entity_registry or EntityRegistry(session)

    @staticmethod
    def is_memory_path(path: str) -> bool:
        """Check if a path is a memory virtual path.

        Detects memory path patterns:
        - /objs/memory/{id}
        - /memory/by-{type}/{id}/...
        - /workspace/{...}/memory/...

        Args:
            path: Virtual path to check.

        Returns:
            True if path is a memory path, False otherwise.
        """
        parts = [p for p in path.split("/") if p]

        # Pattern 1: /objs/memory/{id}
        if len(parts) >= 2 and parts[0] == "objs" and parts[1] == "memory":
            return True

        # Pattern 2: /memory/by-{type}/{id}/...
        # Only match memory API paths (by-user, by-agent, by-zone), not registered memory directories
        if len(parts) >= 2 and parts[0] == "memory" and parts[1].startswith("by-"):
            return True

        # Pattern 3: /workspace/{...}/memory/...
        # Must contain "memory" component and have workspace prefix
        return bool(parts) and parts[0] == "workspace" and "memory" in parts

    def resolve(self, virtual_path: str) -> MemoryModel | None:
        """Resolve virtual path to canonical memory.

        Supports multiple path formats:
        - /workspace/{zone}/{user}/{agent}/memory/{filename}
        - /workspace/{user}/{agent}/memory/{filename}
        - /workspace/{agent}/{user}/memory/{filename}
        - /memory/by-user/{user}/{filename}
        - /memory/by-agent/{agent}/{filename}
        - /objs/memory/{memory_id}

        Args:
            virtual_path: Virtual path to resolve.

        Returns:
            MemoryModel or None if not found.
        """
        # Parse path
        parts = [p for p in virtual_path.split("/") if p]

        # Check if this is a direct canonical path
        if len(parts) >= 3 and parts[0] == "objs" and parts[1] == "memory":
            memory_id = parts[2]
            return self.get_memory_by_id(memory_id)

        # Extract IDs from path (order-independent)
        ids = self._extract_ids(parts)

        # Query by relationships
        return self._query_by_relationships(ids)

    def _extract_ids(self, parts: list[str]) -> dict[str, str]:
        """Extract entity IDs from path parts using entity registry.

        Args:
            parts: List of path components.

        Returns:
            Dictionary mapping entity type keys to IDs.
        """
        return self.entity_registry.extract_ids_from_path_parts(parts)

    def _query_by_relationships(self, ids: dict[str, str]) -> MemoryModel | None:
        """Query memory by identity relationships.

        Args:
            ids: Dictionary of entity IDs (e.g., {'user_id': 'alice', 'agent_id': 'agent1'}).

        Returns:
            MemoryModel or None if not found.
        """
        # If no IDs provided, can't query
        if not ids:
            return None

        # Build query based on available IDs
        stmt = select(MemoryModel)

        # Add filters for each ID type
        # Use OR logic for flexibility - match on any provided ID
        filters = []

        if "zone_id" in ids:
            filters.append(MemoryModel.zone_id == ids["zone_id"])

        if "user_id" in ids:
            filters.append(MemoryModel.user_id == ids["user_id"])

        if "agent_id" in ids:
            filters.append(MemoryModel.agent_id == ids["agent_id"])

        if not filters:
            return None

        # For now, use AND logic (all provided IDs must match)
        # This ensures we get the correct memory when multiple IDs are provided
        for filter_condition in filters:
            stmt = stmt.where(filter_condition)

        # Order by created_at DESC to get most recent memory first
        stmt = stmt.order_by(MemoryModel.created_at.desc())

        # Return first match (most recent memory)
        # Note: If multiple memories match, returns the most recent one
        return self.session.execute(stmt).scalars().first()

    def get_memory_by_id(self, memory_id: str) -> MemoryModel | None:
        """Get memory by canonical ID.

        Args:
            memory_id: Memory ID.

        Returns:
            MemoryModel or None if not found.
        """
        stmt = select(MemoryModel).where(MemoryModel.memory_id == memory_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def query_memories(
        self,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace match
        namespace_prefix: str | None = None,  # v0.8.0: Prefix match for hierarchical queries
        state: str | None = None,  # #368: Filter by state ('inactive', 'active', 'all')
        after: datetime | None = None,  # #1023: Temporal filter - after this time
        before: datetime | None = None,  # #1023: Temporal filter - before this time
        entity_type: str | None = None,  # #1025: Filter by entity type
        person: str | None = None,  # #1025: Filter by person reference
        event_after: datetime | None = None,  # #1028: Filter by event date >= value
        event_before: datetime | None = None,  # #1028: Filter by event date <= value
        include_invalid: bool = False,  # #1183: Include invalidated memories
        valid_at_point: datetime | None = None,  # #1183: Point-in-time query
        limit: int | None = None,
    ) -> list[MemoryModel]:
        """Query memories by relationships and metadata.

        Args:
            zone_id: Filter by zone.
            user_id: Filter by user.
            agent_id: Filter by agent.
            scope: Filter by scope ('agent', 'user', 'zone', 'global').
            memory_type: Filter by memory type.
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix (hierarchical). v0.8.0
            state: Filter by state ('inactive', 'active', 'all'). #368
            after: Filter memories created after this datetime. #1023
            before: Filter memories created before this datetime. #1023
            entity_type: Filter by entity type (e.g., "PERSON", "ORG"). #1025
            person: Filter by person name reference. #1025
            event_after: Filter by event earliest_date >= value. #1028
            event_before: Filter by event latest_date <= value. #1028
            include_invalid: Include invalidated memories (default False). #1183
            valid_at_point: Point-in-time query - return facts valid at this time. #1183
            limit: Maximum number of results.

        Returns:
            List of matching memories.
        """
        stmt = select(MemoryModel)

        if zone_id:
            stmt = stmt.where(MemoryModel.zone_id == zone_id)

        if user_id:
            stmt = stmt.where(MemoryModel.user_id == user_id)

        if agent_id:
            stmt = stmt.where(MemoryModel.agent_id == agent_id)

        if scope:
            stmt = stmt.where(MemoryModel.scope == scope)

        if memory_type:
            stmt = stmt.where(MemoryModel.memory_type == memory_type)

        # v0.8.0: Namespace filtering
        if namespace:
            stmt = stmt.where(MemoryModel.namespace == namespace)
        elif namespace_prefix:
            # Prefix match for hierarchical queries
            stmt = stmt.where(MemoryModel.namespace.like(f"{namespace_prefix}%"))

        # #368: State filtering
        if state and state != "all":
            stmt = stmt.where(MemoryModel.state == state)

        # #1023: Temporal filtering
        if after:
            stmt = stmt.where(MemoryModel.created_at >= after)
        if before:
            stmt = stmt.where(MemoryModel.created_at <= before)

        # #1025: Entity filtering (using LIKE for contains check)
        if entity_type:
            stmt = stmt.where(MemoryModel.entity_types.contains(entity_type))
        if person:
            stmt = stmt.where(MemoryModel.person_refs.contains(person))

        # #1028: Event date filtering (filter by extracted temporal metadata)
        if event_after:
            stmt = stmt.where(MemoryModel.earliest_date >= event_after)
        if event_before:
            stmt = stmt.where(MemoryModel.latest_date <= event_before)

        # #1183: Bi-temporal filtering
        if not include_invalid:
            # Exclude invalidated memories (invalid_at IS NULL = still valid)
            stmt = stmt.where(MemoryModel.invalid_at.is_(None))

        if valid_at_point is not None:
            # Point-in-time query: valid_at <= point AND (invalid_at IS NULL OR invalid_at > point)
            stmt = stmt.where(
                or_(
                    MemoryModel.valid_at.is_(None),  # NULL = use created_at
                    MemoryModel.valid_at <= valid_at_point,
                )
            )
            stmt = stmt.where(
                or_(
                    MemoryModel.invalid_at.is_(None),
                    MemoryModel.invalid_at > valid_at_point,
                )
            )

        # Order by created_at DESC for consistent ordering
        stmt = stmt.order_by(MemoryModel.created_at.desc())

        if limit:
            stmt = stmt.limit(limit)

        return list(self.session.execute(stmt).scalars().all())

    def _create_version_entry(
        self,
        memory_id: str,
        content_hash: str,
        size_bytes: int,
        version_number: int,
        source_type: str = "original",
        parent_version_id: str | None = None,
        change_reason: str | None = None,
        created_by: str | None = None,
    ) -> VersionHistoryModel:
        """Create a version history entry for a memory.

        Args:
            memory_id: The memory ID (resource_id).
            content_hash: SHA-256 hash of the content.
            size_bytes: Size of the content in bytes.
            version_number: Version number for this entry.
            source_type: How version was created ('original', 'update', 'consolidated', 'rollback').
            parent_version_id: ID of the previous version (for lineage tracking).
            change_reason: Description of why version was created.
            created_by: User or agent ID who created this version.

        Returns:
            The created VersionHistoryModel.
        """
        version_entry = VersionHistoryModel(
            version_id=str(uuid.uuid4()),
            resource_type="memory",
            resource_id=memory_id,
            version_number=version_number,
            content_hash=content_hash,
            size_bytes=size_bytes,
            mime_type="application/json",  # Memory content is typically JSON
            parent_version_id=parent_version_id,
            source_type=source_type,
            change_reason=change_reason,
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        version_entry.validate()
        self.session.add(version_entry)
        return version_entry

    def create_memory(
        self,
        content_hash: str,
        zone_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        scope: str = "agent",
        visibility: str = "private",
        memory_type: str | None = None,
        importance: float | None = None,
        namespace: str | None = None,  # v0.8.0: Hierarchical namespace
        path_key: str | None = None,  # v0.8.0: Optional key for upsert mode
        state: str = "active",  # #368: Memory state
        embedding: str | None = None,  # #406: Embedding vector (JSON)
        embedding_model: str | None = None,  # #406: Embedding model name
        embedding_dim: int | None = None,  # #406: Embedding dimension
        entities_json: str | None = None,  # #1025: Entity extraction JSON
        entity_types: str | None = None,  # #1025: Comma-separated entity types
        person_refs: str | None = None,  # #1025: Comma-separated person names
        temporal_refs_json: str | None = None,  # #1028: Temporal refs JSON
        earliest_date: Any = None,  # #1028: Earliest date mentioned
        latest_date: Any = None,  # #1028: Latest date mentioned
        relationships_json: str | None = None,  # #1038: Relationship extraction JSON
        relationship_count: int | None = None,  # #1038: Count of relationships
        valid_at: Any = None,  # #1183: When fact became valid in real world
        size_bytes: int = 0,  # #1184: Content size for version tracking
        created_by: str | None = None,  # #1184: Who created this version
        change_reason: str | None = None,  # #1184: Why this version was created
    ) -> MemoryModel:
        """Create a new memory (or update if path_key exists).

        Args:
            content_hash: SHA-256 hash of content (CAS reference).
            zone_id: Zone ID.
            user_id: User ID (owner). If not provided, defaults to agent_id for backward compatibility.
            agent_id: Agent ID (creator).
            scope: Scope ('agent', 'user', 'zone', 'global').
            visibility: Visibility ('private', 'shared', 'public').
            memory_type: Type of memory ('fact', 'preference', 'experience').
            importance: Importance score (0.0-1.0).
            namespace: Hierarchical namespace (e.g., "knowledge/geography/facts"). v0.8.0
            path_key: Optional unique key within namespace for upsert mode. v0.8.0
            embedding: Vector embedding as JSON string. #406
            embedding_model: Name of embedding model used. #406
            embedding_dim: Dimension of embedding vector. #406
            entities_json: JSON string of extracted entities. #1025
            entity_types: Comma-separated entity types (e.g., "PERSON,ORG,DATE"). #1025
            person_refs: Comma-separated person names for quick filtering. #1025
            temporal_refs_json: JSON string of extracted temporal references. #1028
            earliest_date: Earliest date mentioned in content. #1028
            latest_date: Latest date mentioned in content. #1028
            valid_at: When fact became valid in real world (NULL = use created_at). #1183

        Returns:
            MemoryModel: Created or updated memory.
        """
        # v0.4.0: Fallback for backward compatibility
        # If user_id is not provided, use agent_id as user_id
        if user_id is None and agent_id is not None:
            user_id = agent_id

        # v0.8.0: Upsert logic - check if memory with namespace+path_key exists
        existing_memory = None
        if namespace and path_key:
            stmt = select(MemoryModel).where(
                MemoryModel.namespace == namespace,
                MemoryModel.path_key == path_key,
                MemoryModel.user_id == user_id,  # Scope to same user
            )
            # Filter by zone if provided
            if zone_id:
                stmt = stmt.where(MemoryModel.zone_id == zone_id)
            existing_memory = self.session.execute(stmt).scalar_one_or_none()

        if existing_memory:
            # #1184: Get current version entry for lineage tracking
            prev_version_stmt = select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "memory",
                VersionHistoryModel.resource_id == existing_memory.memory_id,
                VersionHistoryModel.version_number == existing_memory.current_version,
            )
            prev_version_entry = self.session.execute(prev_version_stmt).scalar_one_or_none()
            prev_version_id = prev_version_entry.version_id if prev_version_entry else None

            # Update existing memory
            existing_memory.content_hash = content_hash
            existing_memory.scope = scope
            existing_memory.visibility = visibility
            existing_memory.memory_type = memory_type
            existing_memory.importance = importance
            # Update other fields if provided
            if zone_id is not None:
                existing_memory.zone_id = zone_id
            if user_id is not None:
                existing_memory.user_id = user_id
            if agent_id is not None:
                existing_memory.agent_id = agent_id
            # Update embedding fields (#406)
            if embedding is not None:
                existing_memory.embedding = embedding
            if embedding_model is not None:
                existing_memory.embedding_model = embedding_model
            if embedding_dim is not None:
                existing_memory.embedding_dim = embedding_dim
            # Update entity extraction fields (#1025)
            if entities_json is not None:
                existing_memory.entities_json = entities_json
            if entity_types is not None:
                existing_memory.entity_types = entity_types
            if person_refs is not None:
                existing_memory.person_refs = person_refs
            # Update temporal metadata fields (#1028)
            if temporal_refs_json is not None:
                existing_memory.temporal_refs_json = temporal_refs_json
            if earliest_date is not None:
                existing_memory.earliest_date = earliest_date
            if latest_date is not None:
                existing_memory.latest_date = latest_date
            # Update relationship extraction fields (#1038)
            if relationships_json is not None:
                existing_memory.relationships_json = relationships_json
            if relationship_count is not None:
                existing_memory.relationship_count = relationship_count
            # Update bi-temporal validity (#1183)
            if valid_at is not None:
                existing_memory.valid_at = valid_at

            # #1184: Atomically increment version at database level
            self.session.execute(
                update(MemoryModel)
                .where(MemoryModel.memory_id == existing_memory.memory_id)
                .values(current_version=MemoryModel.current_version + 1)
            )
            self.session.refresh(existing_memory)

            existing_memory.validate()
            self.session.commit()

            # #1184: Create version history entry for the update
            self._create_version_entry(
                memory_id=existing_memory.memory_id,
                content_hash=content_hash,
                size_bytes=size_bytes,
                version_number=existing_memory.current_version,
                source_type="update",
                parent_version_id=prev_version_id,
                change_reason=change_reason or "Memory updated",
                created_by=created_by or user_id or agent_id,
            )
            self.session.commit()

            return existing_memory
        else:
            # Create new memory
            memory = MemoryModel(
                content_hash=content_hash,
                zone_id=zone_id,
                user_id=user_id,
                agent_id=agent_id,
                scope=scope,
                visibility=visibility,
                memory_type=memory_type,
                importance=importance,
                state=state,  # #368: Use provided state (defaults to active for backward compatibility)
                namespace=namespace,
                path_key=path_key,
                embedding=embedding,  # #406
                embedding_model=embedding_model,  # #406
                embedding_dim=embedding_dim,  # #406
                entities_json=entities_json,  # #1025
                entity_types=entity_types,  # #1025
                person_refs=person_refs,  # #1025
                temporal_refs_json=temporal_refs_json,  # #1028
                earliest_date=earliest_date,  # #1028
                latest_date=latest_date,  # #1028
                relationships_json=relationships_json,  # #1038
                relationship_count=relationship_count,  # #1038
                valid_at=valid_at,  # #1183
            )

            # Validate before adding
            memory.validate()

            self.session.add(memory)
            self.session.commit()

            # #1184: Create version 1 history entry
            self._create_version_entry(
                memory_id=memory.memory_id,
                content_hash=content_hash,
                size_bytes=size_bytes,
                version_number=1,
                source_type="original",
                parent_version_id=None,
                change_reason=change_reason or "Memory created",
                created_by=created_by or user_id or agent_id,
            )
            self.session.commit()

            # Create ReBAC tuple for memory owner (v0.6.0 pure ReBAC)
            # Grant owner full access to their memory
            if user_id:
                from sqlalchemy import Engine

                from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

                bind = self.session.get_bind()
                assert isinstance(bind, Engine), "Expected Engine, got Connection"
                rebac = EnhancedReBACManager(bind)

                # Grant owner permission to the memory
                rebac.rebac_write(
                    subject=("user", user_id),
                    relation="owner",
                    object=("memory", memory.memory_id),
                    zone_id=zone_id,
                )

            return memory

    def update_memory(
        self,
        memory_id: str,
        **updates: dict,
    ) -> MemoryModel | None:
        """Update a memory.

        Args:
            memory_id: Memory ID.
            **updates: Fields to update.

        Returns:
            Updated MemoryModel or None if not found.
        """
        memory = self.get_memory_by_id(memory_id)
        if not memory:
            return None

        for key, value in updates.items():
            if hasattr(memory, key):
                setattr(memory, key, value)

        # Validate after updates
        memory.validate()

        self.session.commit()
        return memory

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory.

        Args:
            memory_id: Memory ID.

        Returns:
            True if deleted, False if not found.
        """
        memory = self.get_memory_by_id(memory_id)
        if memory:
            self.session.delete(memory)
            self.session.commit()
            return True
        return False

    def invalidate_memory(self, memory_id: str, invalid_at: datetime) -> MemoryModel | None:
        """Invalidate a memory (set invalid_at timestamp) (#1183).

        This is a temporal soft-delete that marks when a fact became false,
        without removing the historical record.

        Args:
            memory_id: Memory ID to invalidate.
            invalid_at: When the fact became invalid.

        Returns:
            Updated MemoryModel or None if not found.
        """
        memory = self.get_memory_by_id(memory_id)
        if not memory:
            return None

        memory.invalid_at = invalid_at
        memory.validate()
        self.session.commit()
        return memory

    def revalidate_memory(self, memory_id: str) -> MemoryModel | None:
        """Revalidate a memory (clear invalid_at timestamp) (#1183).

        Use when a previously invalidated fact becomes true again.

        Args:
            memory_id: Memory ID to revalidate.

        Returns:
            Updated MemoryModel or None if not found.
        """
        memory = self.get_memory_by_id(memory_id)
        if not memory:
            return None

        memory.invalid_at = None
        memory.validate()
        self.session.commit()
        return memory

    def update_memory_state(self, memory_id: str, state: str) -> MemoryModel | None:
        """Update memory state (#368).

        Args:
            memory_id: Memory ID.
            state: New state ('inactive', 'active').

        Returns:
            Updated MemoryModel or None if not found.
        """
        memory = self.get_memory_by_id(memory_id)
        if not memory:
            return None

        memory.state = state
        memory.validate()
        self.session.commit()
        return memory

    def approve_memory(self, memory_id: str) -> MemoryModel | None:
        """Approve a memory (set state to active) (#368).

        Args:
            memory_id: Memory ID to approve.

        Returns:
            Updated MemoryModel or None if not found.
        """
        return self.update_memory_state(memory_id, "active")

    def deactivate_memory(self, memory_id: str) -> MemoryModel | None:
        """Deactivate a memory (set state to inactive) (#368).

        Args:
            memory_id: Memory ID to deactivate.

        Returns:
            Updated MemoryModel or None if not found.
        """
        return self.update_memory_state(memory_id, "inactive")

    def get_virtual_paths(self, memory: MemoryModel) -> list[str]:
        """Generate all valid virtual paths for a memory.

        Args:
            memory: Memory instance.

        Returns:
            List of virtual path strings.
        """
        paths = []

        # Canonical path
        paths.append(f"/objs/memory/{memory.memory_id}")

        # Workspace paths (all permutations if IDs exist)
        if memory.zone_id and memory.user_id and memory.agent_id:
            paths.append(f"/workspace/{memory.zone_id}/{memory.user_id}/{memory.agent_id}/memory/")
            paths.append(f"/workspace/{memory.zone_id}/{memory.agent_id}/{memory.user_id}/memory/")
            paths.append(f"/workspace/{memory.user_id}/{memory.zone_id}/{memory.agent_id}/memory/")
            paths.append(f"/workspace/{memory.user_id}/{memory.agent_id}/{memory.zone_id}/memory/")
            paths.append(f"/workspace/{memory.agent_id}/{memory.user_id}/{memory.zone_id}/memory/")
            paths.append(f"/workspace/{memory.agent_id}/{memory.zone_id}/{memory.user_id}/memory/")

        elif memory.user_id and memory.agent_id:
            paths.append(f"/workspace/{memory.user_id}/{memory.agent_id}/memory/")
            paths.append(f"/workspace/{memory.agent_id}/{memory.user_id}/memory/")

        # By-user path
        if memory.user_id:
            paths.append(f"/memory/by-user/{memory.user_id}/")

        # By-agent path
        if memory.agent_id:
            paths.append(f"/memory/by-agent/{memory.agent_id}/")

        # By-zone path
        if memory.zone_id:
            paths.append(f"/memory/by-zone/{memory.zone_id}/")

        return paths
