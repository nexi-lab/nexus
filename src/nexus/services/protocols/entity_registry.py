"""Entity registry service protocol (Issue #2133).

Service contract for entity registration and lookup.
Existing implementation: ``nexus.bricks.rebac.entity_registry.EntityRegistry`` (sync).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - Issue #2133: Break circular runtime imports between services/ and core/
    - Issue #2359: Moved from core/protocols/ to services/protocols/ (service tier)
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EntityRegistryProtocol(Protocol):
    """Service contract for entity registration and lookup.

    Do NOT use ``isinstance()`` checks in hot paths — use structural
    typing via Protocol matching instead.
    """

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        parent_type: str | None = None,
        parent_id: str | None = None,
        entity_metadata: dict[str, Any] | None = None,
    ) -> Any: ...

    def get_entity(
        self,
        entity_type: str,
        entity_id: str,
    ) -> Any | None: ...

    def lookup_entity_by_id(
        self,
        entity_id: str,
    ) -> list[Any]: ...

    def get_entities_by_type(
        self,
        entity_type: str,
    ) -> list[Any]: ...

    def get_children(
        self,
        parent_type: str,
        parent_id: str,
    ) -> list[Any]: ...

    def delete_entity(
        self,
        entity_type: str,
        entity_id: str,
        cascade: bool = True,
    ) -> bool: ...
