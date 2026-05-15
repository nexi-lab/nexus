"""Aspect service protocol — contract for entity metadata operations (Issue #2929).

Defines the service interface for reading/writing aspects on entities
identified by URN. Separate from EntityRegistryProtocol (entity lifecycle).

Storage Affinity: **RecordStore** (entity_aspects table with version-0 pattern).
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AspectServiceProtocol(Protocol):
    """Service contract for entity aspect CRUD operations.

    Aspects are versioned metadata facets keyed by (entity_urn, aspect_name).
    Uses the DataHub version-0 pattern: version 0 is always current.
    """

    def get_aspect(
        self,
        entity_urn: str,
        aspect_name: str,
    ) -> dict[str, Any] | None:
        """Get the current (version 0) aspect for an entity.

        Args:
            entity_urn: URN of the entity.
            aspect_name: Registered aspect type name.

        Returns:
            Aspect payload dict, or None if not found.
        """
        ...

    def get_aspect_version(
        self,
        entity_urn: str,
        aspect_name: str,
        version: int,
    ) -> dict[str, Any] | None:
        """Get a specific version of an aspect.

        Args:
            entity_urn: URN of the entity.
            aspect_name: Aspect type name.
            version: Version number (0 = current, 1+ = history).

        Returns:
            Aspect payload dict, or None if not found.
        """
        ...

    def put_aspect(
        self,
        entity_urn: str,
        aspect_name: str,
        payload: dict[str, Any],
        *,
        created_by: str = "system",
        zone_id: str | None = None,
    ) -> int:
        """Create or update an aspect (version-0 swap pattern).

        If the aspect exists:
            1. Copy current (version 0) to version N+1
            2. Overwrite version 0 with new payload
            3. Compact old versions beyond max_versions
        If new:
            1. Insert version 0

        Args:
            entity_urn: URN of the entity.
            aspect_name: Registered aspect type name.
            payload: JSON-serializable aspect data.
            created_by: User/agent performing the update.
            zone_id: Zone scope for MCL recording.

        Returns:
            The new version number assigned to the historical copy.

        Raises:
            ValueError: If aspect_name is not registered or payload too large.
        """
        ...

    def delete_aspect(
        self,
        entity_urn: str,
        aspect_name: str,
        *,
        zone_id: str | None = None,
    ) -> bool:
        """Soft-delete an aspect (mark all versions as deleted).

        Args:
            entity_urn: URN of the entity.
            aspect_name: Aspect type name.
            zone_id: Zone scope for MCL recording.

        Returns:
            True if the aspect existed and was deleted.
        """
        ...

    def list_aspects(
        self,
        entity_urn: str,
    ) -> list[str]:
        """List all current aspect names for an entity.

        Args:
            entity_urn: URN of the entity.

        Returns:
            List of aspect names that have a current (version 0) value.
        """
        ...

    def get_aspects_batch(
        self,
        entity_urns: list[str],
        aspect_name: str,
    ) -> dict[str, dict[str, Any]]:
        """Batch-load current aspects for multiple entities (N+1 prevention).

        Args:
            entity_urns: List of entity URNs.
            aspect_name: Aspect type to load.

        Returns:
            Dict mapping entity_urn → payload for entities that have the aspect.
        """
        ...

    def soft_delete_entity_aspects(
        self,
        entity_urn: str,
    ) -> int:
        """Soft-delete all aspects for an entity (cascade on entity delete).

        Args:
            entity_urn: URN of the entity being deleted.

        Returns:
            Number of aspects soft-deleted.
        """
        ...

    def find_entities_with_aspect(
        self,
        aspect_name: str,
    ) -> dict[str, dict[str, Any]]:
        """Find all entities that have a given aspect (current version).

        Used for scan-based searches (e.g., search_by_column).
        Production should use a search index built from MCL events.

        Args:
            aspect_name: Aspect type to search for.

        Returns:
            Dict mapping entity_urn → payload for all entities with this aspect.
        """
        ...

    def get_aspect_history(
        self,
        entity_urn: str,
        aspect_name: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get version history for an aspect.

        Args:
            entity_urn: URN of the entity.
            aspect_name: Aspect type name.
            limit: Max versions to return.

        Returns:
            List of aspect versions, newest first. Each dict includes
            'version', 'payload', 'created_by', 'created_at'.
        """
        ...
