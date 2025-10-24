"""Memory API for AI Agent Memory Management (v0.4.0).

High-level API for storing, querying, and searching agent memories
with identity-based relationships and semantic search.
"""

from typing import Any

from sqlalchemy.orm import Session

from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.core.memory_router import MemoryViewRouter
from nexus.core.permissions import OperationContext, Permission


class Memory:
    """High-level Memory API for AI agents.

    Provides simple methods for storing, querying, and searching memories
    with automatic permission checks and identity management.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        tenant_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        entity_registry: EntityRegistry | None = None,
    ):
        """Initialize Memory API.

        Args:
            session: Database session.
            backend: Storage backend for content.
            tenant_id: Current tenant ID.
            user_id: Current user ID.
            agent_id: Current agent ID.
            entity_registry: Entity registry instance.
        """
        self.session = session
        self.backend = backend
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.agent_id = agent_id

        # Initialize components
        self.entity_registry = entity_registry or EntityRegistry(session)
        self.memory_router = MemoryViewRouter(session, self.entity_registry)
        self.permission_enforcer = MemoryPermissionEnforcer(
            memory_router=self.memory_router,
            entity_registry=self.entity_registry,
        )

        # Create operation context
        self.context = OperationContext(
            user=agent_id or user_id or "system",
            groups=[],
            is_admin=False,
        )

    def store(
        self,
        content: str | bytes,
        scope: str = "user",
        memory_type: str | None = None,
        importance: float | None = None,
        _metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store a memory.

        Args:
            content: Memory content (text or bytes).
            scope: Memory scope ('agent', 'user', 'tenant', 'global').
            memory_type: Type of memory ('fact', 'preference', 'experience').
            importance: Importance score (0.0-1.0).
            metadata: Additional metadata.

        Returns:
            memory_id: The created memory ID.

        Example:
            >>> memory = nx.memory
            >>> memory_id = memory.store(
            ...     "User prefers Python over JavaScript",
            ...     scope="user",
            ...     memory_type="preference",
            ...     importance=0.9
            ... )
        """
        # Convert content to bytes
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content

        # Store content in backend (CAS)
        # LocalBackend.write_content() handles hashing and storage
        try:
            content_hash = self.backend.write_content(content_bytes)
        except Exception as e:
            # If backend write fails, we can't proceed
            raise RuntimeError(f"Failed to store content in backend: {e}") from e

        # Create memory record
        memory = self.memory_router.create_memory(
            content_hash=content_hash,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            scope=scope,
            memory_type=memory_type,
            importance=importance,
        )

        return memory.memory_id

    def query(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query memories by relationships and metadata.

        Args:
            user_id: Filter by user ID (defaults to current user).
            agent_id: Filter by agent ID.
            tenant_id: Filter by tenant ID (defaults to current tenant).
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.

        Returns:
            List of memory dictionaries with metadata.

        Example:
            >>> memories = memory.query(scope="user", memory_type="preference")
            >>> for mem in memories:
            ...     print(f"{mem['memory_id']}: {mem['content']}")
        """
        # Use current context if not specified
        if user_id is None:
            user_id = self.user_id
        if tenant_id is None:
            tenant_id = self.tenant_id

        # Query memories
        memories = self.memory_router.query_memories(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

        # Filter by permissions and enrich with content
        results = []
        for memory in memories:
            # Check read permission
            if not self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
                continue

            # Read content from CAS
            content = None
            try:
                content_bytes = self.backend.read_content(memory.content_hash)
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = content_bytes.hex()  # Binary content
            except Exception:
                content = f"<content not available: {memory.content_hash}>"

            results.append(
                {
                    "memory_id": memory.memory_id,
                    "content": content,
                    "content_hash": memory.content_hash,
                    "tenant_id": memory.tenant_id,
                    "user_id": memory.user_id,
                    "agent_id": memory.agent_id,
                    "scope": memory.scope,
                    "visibility": memory.visibility,
                    "memory_type": memory.memory_type,
                    "importance": memory.importance,
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                    "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                }
            )

        return results

    def search(
        self,
        query: str,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Semantic search over memories.

        Args:
            query: Search query text.
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.

        Returns:
            List of memory dictionaries with relevance scores.

        Example:
            >>> results = memory.search("Python programming preferences")
            >>> for mem in results:
            ...     print(f"Score: {mem['score']:.2f} - {mem['content']}")

        Note:
            Semantic search requires vector embeddings. If not available,
            falls back to simple text matching.
        """
        # TODO: Implement vector-based semantic search
        # For now, fall back to query-based filtering with text matching

        # Get all memories matching filters
        memories = self.query(
            scope=scope,
            memory_type=memory_type,
            limit=limit * 3,  # Get more to filter
        )

        # Simple text matching (fallback until vector search is integrated)
        query_lower = query.lower()
        scored_results = []

        for memory in memories:
            content = memory.get("content", "")
            if not content or isinstance(content, bytes):
                continue

            content_lower = str(content).lower()

            # Simple relevance scoring
            score = 0.0
            if query_lower in content_lower:
                score = 1.0
            else:
                # Count word matches
                query_words = query_lower.split()
                matches = sum(1 for word in query_words if word in content_lower)
                score = matches / len(query_words) if query_words else 0.0

            if score > 0:
                memory["score"] = score
                scored_results.append(memory)

        # Sort by score and limit
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        return scored_results[:limit]

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """Get a specific memory by ID.

        Args:
            memory_id: Memory ID.

        Returns:
            Memory dictionary or None if not found or no permission.

        Example:
            >>> mem = memory.get("mem_123")
            >>> print(mem['content'])
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return None

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
            return None

        # Read content
        content = None
        try:
            content_bytes = self.backend.read_content(memory.content_hash)
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                content = content_bytes.hex()
        except Exception:
            content = f"<content not available: {memory.content_hash}>"

        return {
            "memory_id": memory.memory_id,
            "content": content,
            "content_hash": memory.content_hash,
            "tenant_id": memory.tenant_id,
            "user_id": memory.user_id,
            "agent_id": memory.agent_id,
            "scope": memory.scope,
            "visibility": memory.visibility,
            "memory_type": memory.memory_type,
            "importance": memory.importance,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
        }

    def delete(self, memory_id: str) -> bool:
        """Delete a memory.

        Args:
            memory_id: Memory ID to delete.

        Returns:
            True if deleted, False if not found or no permission.

        Example:
            >>> memory.delete("mem_123")
            True
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, self.context):
            return False

        return self.memory_router.delete_memory(memory_id)

    def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int | None = 100,
    ) -> list[dict[str, Any]]:
        """List memories for current user/agent.

        Args:
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.

        Returns:
            List of memory dictionaries (without full content for efficiency).

        Example:
            >>> memories = memory.list(scope="user")
            >>> print(f"Found {len(memories)} memories")
        """
        memories = self.memory_router.query_memories(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

        results = []
        for memory in memories:
            # Check permission
            if not self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
                continue

            results.append(
                {
                    "memory_id": memory.memory_id,
                    "content_hash": memory.content_hash,
                    "tenant_id": memory.tenant_id,
                    "user_id": memory.user_id,
                    "agent_id": memory.agent_id,
                    "scope": memory.scope,
                    "visibility": memory.visibility,
                    "memory_type": memory.memory_type,
                    "importance": memory.importance,
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                    "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                }
            )

        return results
