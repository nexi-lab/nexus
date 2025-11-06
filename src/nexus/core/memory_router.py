"""Memory View Router for Order-Neutral Paths (v0.4.0).

Resolves virtual paths to canonical memory IDs regardless of path order.
Enables multiple virtual path views for the same memory.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.core.entity_registry import EntityRegistry
from nexus.storage.models import MemoryModel


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
        # Only match memory API paths (by-user, by-agent, by-tenant), not registered memory directories
        if len(parts) >= 2 and parts[0] == "memory" and parts[1].startswith("by-"):
            return True

        # Pattern 3: /workspace/{...}/memory/...
        # Must contain "memory" component and have workspace prefix
        return bool(parts) and parts[0] == "workspace" and "memory" in parts

    def resolve(self, virtual_path: str) -> MemoryModel | None:
        """Resolve virtual path to canonical memory.

        Supports multiple path formats:
        - /workspace/{tenant}/{user}/{agent}/memory/{filename}
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

        if "tenant_id" in ids:
            filters.append(MemoryModel.tenant_id == ids["tenant_id"])

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
        tenant_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace match
        namespace_prefix: str | None = None,  # v0.8.0: Prefix match for hierarchical queries
        state: str | None = None,  # #368: Filter by state ('inactive', 'active', 'all')
        limit: int | None = None,
    ) -> list[MemoryModel]:
        """Query memories by relationships and metadata.

        Args:
            tenant_id: Filter by tenant.
            user_id: Filter by user.
            agent_id: Filter by agent.
            scope: Filter by scope ('agent', 'user', 'tenant', 'global').
            memory_type: Filter by memory type.
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix (hierarchical). v0.8.0
            state: Filter by state ('inactive', 'active', 'all'). #368
            limit: Maximum number of results.

        Returns:
            List of matching memories.
        """
        stmt = select(MemoryModel)

        if tenant_id:
            stmt = stmt.where(MemoryModel.tenant_id == tenant_id)

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

        if limit:
            stmt = stmt.limit(limit)

        return list(self.session.execute(stmt).scalars().all())

    def create_memory(
        self,
        content_hash: str,
        tenant_id: str | None = None,
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
    ) -> MemoryModel:
        """Create a new memory (or update if path_key exists).

        Args:
            content_hash: SHA-256 hash of content (CAS reference).
            tenant_id: Tenant ID.
            user_id: User ID (owner). If not provided, defaults to agent_id for backward compatibility.
            agent_id: Agent ID (creator).
            scope: Scope ('agent', 'user', 'tenant', 'global').
            visibility: Visibility ('private', 'shared', 'public').
            memory_type: Type of memory ('fact', 'preference', 'experience').
            importance: Importance score (0.0-1.0).
            namespace: Hierarchical namespace (e.g., "knowledge/geography/facts"). v0.8.0
            path_key: Optional unique key within namespace for upsert mode. v0.8.0
            embedding: Vector embedding as JSON string. #406
            embedding_model: Name of embedding model used. #406
            embedding_dim: Dimension of embedding vector. #406

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
            # Filter by tenant if provided
            if tenant_id:
                stmt = stmt.where(MemoryModel.tenant_id == tenant_id)
            existing_memory = self.session.execute(stmt).scalar_one_or_none()

        if existing_memory:
            # Update existing memory
            existing_memory.content_hash = content_hash
            existing_memory.scope = scope
            existing_memory.visibility = visibility
            existing_memory.memory_type = memory_type
            existing_memory.importance = importance
            # Update other fields if provided
            if tenant_id is not None:
                existing_memory.tenant_id = tenant_id
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

            existing_memory.validate()
            self.session.commit()
            return existing_memory
        else:
            # Create new memory
            memory = MemoryModel(
                content_hash=content_hash,
                tenant_id=tenant_id,
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
            )

            # Validate before adding
            memory.validate()

            self.session.add(memory)
            self.session.commit()

            # Create ReBAC tuple for memory owner (v0.6.0 pure ReBAC)
            # Grant owner full access to their memory
            if user_id:
                from sqlalchemy import Engine

                from nexus.core.rebac_manager import ReBACManager

                bind = self.session.get_bind()
                assert isinstance(bind, Engine), "Expected Engine, got Connection"
                rebac = ReBACManager(bind)

                # Grant owner permission to the memory
                rebac.rebac_write(
                    subject=("user", user_id),
                    relation="owner",
                    object=("memory", memory.memory_id),
                    tenant_id=tenant_id,
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
        if memory.tenant_id and memory.user_id and memory.agent_id:
            paths.append(
                f"/workspace/{memory.tenant_id}/{memory.user_id}/{memory.agent_id}/memory/"
            )
            paths.append(
                f"/workspace/{memory.tenant_id}/{memory.agent_id}/{memory.user_id}/memory/"
            )
            paths.append(
                f"/workspace/{memory.user_id}/{memory.tenant_id}/{memory.agent_id}/memory/"
            )
            paths.append(
                f"/workspace/{memory.user_id}/{memory.agent_id}/{memory.tenant_id}/memory/"
            )
            paths.append(
                f"/workspace/{memory.agent_id}/{memory.user_id}/{memory.tenant_id}/memory/"
            )
            paths.append(
                f"/workspace/{memory.agent_id}/{memory.tenant_id}/{memory.user_id}/memory/"
            )

        elif memory.user_id and memory.agent_id:
            paths.append(f"/workspace/{memory.user_id}/{memory.agent_id}/memory/")
            paths.append(f"/workspace/{memory.agent_id}/{memory.user_id}/memory/")

        # By-user path
        if memory.user_id:
            paths.append(f"/memory/by-user/{memory.user_id}/")

        # By-agent path
        if memory.agent_id:
            paths.append(f"/memory/by-agent/{memory.agent_id}/")

        # By-tenant path
        if memory.tenant_id:
            paths.append(f"/memory/by-tenant/{memory.tenant_id}/")

        return paths
