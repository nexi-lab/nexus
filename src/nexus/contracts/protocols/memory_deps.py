"""Narrow protocol dependencies for the Memory brick (Issue #2177).

Replaces the broad MemoryPermissionEnforcer and EntityRegistry dependencies
with 2 narrow protocols that describe exactly what the Memory brick needs:

- MemoryPermissionProtocol: permission checks for memory access
- MemoryEntityRegistryProtocol: entity registration and lookup

These protocols allow the Memory brick to be tested in isolation with
in-memory fakes (see bricks/memory/testing.py), following the same pattern
as SkillPermissionProtocol (Issue #2035).
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryPermissionProtocol(Protocol):
    """Permission operations needed by the Memory brick.

    Narrow contract covering only the two methods Memory actually calls:
    - check_memory: verify a user can READ/WRITE a specific memory
    - create_entity_tuples: register ReBAC ownership tuples for new memories
    """

    def check_memory(self, memory: Any, permission: Any, context: Any) -> bool:
        """Check if context has permission to access memory.

        Args:
            memory: MemoryModel instance.
            permission: Permission enum value (READ, WRITE, EXECUTE).
            context: OperationContext with user identity.

        Returns:
            True if permission is granted.
        """
        ...

    def create_entity_tuples(
        self,
        memory_id: str,
        zone_id: str | None,
        user_id: str | None,
        agent_id: str | None,
    ) -> None:
        """Create ReBAC ownership tuples for a new memory.

        Args:
            memory_id: The new memory's ID.
            zone_id: Zone scope for the tuple.
            user_id: Owner user ID.
            agent_id: Creator agent ID.
        """
        ...


@runtime_checkable
class MemoryEntityRegistryProtocol(Protocol):
    """Entity registry operations needed by the Memory brick.

    Narrow contract covering entity registration and lookup used by
    MemoryViewRouter for path resolution and identity management.
    """

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        parent_type: str | None = None,
        parent_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Register an entity in the registry.

        Args:
            entity_type: Type of entity (e.g., "zone", "user", "agent").
            entity_id: Unique identifier for the entity.
            parent_type: Optional parent entity type.
            parent_id: Optional parent entity ID.

        Returns:
            The registered entity.
        """
        ...

    def extract_ids_from_path_parts(self, parts: list[str]) -> dict[str, str]:
        """Extract entity IDs from path components.

        Args:
            parts: List of path segments to resolve.

        Returns:
            Dictionary mapping entity type keys to IDs.
        """
        ...

    def lookup_entity_by_id(self, entity_id: str) -> list[Any]:
        """Look up entities by ID.

        Args:
            entity_id: The entity ID to look up.

        Returns:
            List of matching entity records.
        """
        ...
