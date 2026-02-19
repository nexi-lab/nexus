"""Abstract cache interfaces for Nexus caching layer.

This module defines protocols (interfaces) that all cache backends must implement.
Using Protocol allows for structural subtyping without requiring inheritance.
"""

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class PermissionCacheProtocol(Protocol):
    """Protocol for permission cache backends.

    Implementations must provide async methods for:
    - Getting cached permission results
    - Setting permission results with TTL
    - Invalidating entries by subject or object
    - Health checking

    Example:
        class MyCache(PermissionCacheProtocol):
            async def get(self, ...) -> bool | None:
                ...
    """

    async def get(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool | None:
        """Get cached permission result.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            object_type: Type of object (e.g., "file", "workspace")
            object_id: ID of object (e.g., "/workspace/file.txt")
            zone_id: Zone ID for multi-tenancy

        Returns:
            True if permission granted, False if denied, None if not in cache
        """
        ...

    async def set(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        result: bool,
        zone_id: str,
    ) -> None:
        """Cache permission result.

        The TTL should be shorter for denials (security) than grants.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission checked
            object_type: Type of object
            object_id: ID of object
            result: True if granted, False if denied
            zone_id: Zone ID
        """
        ...

    async def invalidate_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate all cached permissions for a subject.

        Called when a subject's permissions change (e.g., removed from group).

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            zone_id: Zone ID

        Returns:
            Number of entries invalidated
        """
        ...

    async def invalidate_object(
        self,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate all cached permissions for an object.

        Called when an object's permissions change (e.g., ACL updated).

        Args:
            object_type: Type of object
            object_id: ID of object
            zone_id: Zone ID

        Returns:
            Number of entries invalidated
        """
        ...

    async def invalidate_subject_object(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate cached permissions for a specific subject-object pair.

        More precise than invalidate_subject or invalidate_object.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            object_type: Type of object
            object_id: ID of object
            zone_id: Zone ID

        Returns:
            Number of entries invalidated
        """
        ...

    async def clear(self, zone_id: str | None = None) -> int:
        """Clear all cached permissions.

        Args:
            zone_id: If provided, only clear entries for this zone.
                       If None, clear all entries.

        Returns:
            Number of entries cleared
        """
        ...

    async def health_check(self) -> bool:
        """Check if the cache backend is healthy and responding.

        Returns:
            True if healthy, False otherwise
        """
        ...

    async def get_stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dict with stats like hits, misses, size, etc.
        """
        ...


@runtime_checkable
class TigerCacheProtocol(Protocol):
    """Protocol for Tiger cache backends.

    Tiger cache stores pre-materialized permission bitmaps for O(1) list filtering.
    Each bitmap represents all resources a subject can access with a given permission.

    Example:
        # Check if user can access resource using bitmap
        bitmap_data = await cache.get_bitmap("user", "alice", "read", "file", "zone1")
        if bitmap_data:
            bitmap = RoaringBitmap.deserialize(bitmap_data[0])
            can_access = resource_int_id in bitmap
    """

    async def get_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> tuple[bytes, int] | None:
        """Get Tiger bitmap for a subject.

        Args:
            subject_type: Type of subject (e.g., "user")
            subject_id: ID of subject (e.g., "alice")
            permission: Permission type (e.g., "read")
            resource_type: Type of resources in bitmap (e.g., "file")
            zone_id: Zone ID

        Returns:
            Tuple of (bitmap_data, revision) if found, None otherwise.
            bitmap_data is serialized Roaring Bitmap bytes.
            revision is used for staleness detection.
        """
        ...

    async def set_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        bitmap_data: bytes,
        revision: int,
    ) -> None:
        """Store Tiger bitmap for a subject.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission type
            resource_type: Type of resources
            zone_id: Zone ID
            bitmap_data: Serialized Roaring Bitmap bytes
            revision: Current revision for staleness detection
        """
        ...

    async def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate Tiger cache entries matching criteria.

        All parameters are optional filters. If all are None, clears everything.

        Args:
            subject_type: Filter by subject type
            subject_id: Filter by subject ID
            permission: Filter by permission
            resource_type: Filter by resource type
            zone_id: Filter by zone

        Returns:
            Number of entries invalidated
        """
        ...

    async def health_check(self) -> bool:
        """Check if the cache backend is healthy.

        Returns:
            True if healthy, False otherwise
        """
        ...


@runtime_checkable
class ResourceMapCacheProtocol(Protocol):
    """Protocol for resource map cache (UUID -> int64 mappings).

    Tiger cache uses integer IDs for Roaring Bitmap compatibility.
    This cache stores the mapping between resource UUIDs and integer IDs.
    """

    async def get_int_id(
        self,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> int | None:
        """Get integer ID for a resource.

        Args:
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of resource (e.g., "/workspace/file.txt")
            zone_id: Zone ID

        Returns:
            Integer ID if found, None otherwise
        """
        ...

    async def get_int_ids_bulk(
        self,
        resources: list[tuple[str, str, str]],  # (resource_type, resource_id, zone_id)
    ) -> dict[tuple[str, str, str], int | None]:
        """Bulk get integer IDs for multiple resources.

        Args:
            resources: List of (resource_type, resource_id, zone_id) tuples

        Returns:
            Dict mapping resource tuples to their integer IDs (None if not found)
        """
        ...

    async def set_int_id(
        self,
        resource_type: str,
        resource_id: str,
        zone_id: str,
        int_id: int,
    ) -> None:
        """Store integer ID for a resource.

        Args:
            resource_type: Type of resource
            resource_id: String ID of resource
            zone_id: Zone ID
            int_id: Integer ID to store
        """
        ...

    async def set_int_ids_bulk(
        self,
        mappings: dict[tuple[str, str, str], int],  # (type, id, zone) -> int_id
    ) -> None:
        """Bulk store integer IDs for multiple resources.

        Args:
            mappings: Dict mapping (resource_type, resource_id, zone_id) to integer IDs
        """
        ...


@runtime_checkable
class EmbeddingCacheProtocol(Protocol):
    """Protocol for embedding vector caches.

    Caches embedding vectors by content hash to avoid redundant API calls.
    Supports batch operations with deduplication for efficiency.
    """

    async def get(self, text: str, model: str) -> list[float] | None:
        """Get cached embedding for text.

        Returns:
            Embedding vector if cached, None otherwise.
        """
        ...

    async def set(self, text: str, model: str, embedding: list[float]) -> None:
        """Cache embedding for text."""
        ...

    async def get_batch(self, texts: list[str], model: str) -> dict[str, list[float] | None]:
        """Get cached embeddings for multiple texts.

        Returns:
            Dict mapping text -> embedding (None if not cached).
        """
        ...

    async def set_batch(self, embeddings: dict[str, list[float]], model: str) -> None:
        """Cache multiple embeddings."""
        ...

    async def get_or_embed_batch(
        self,
        texts: list[str],
        model: str,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> list[list[float]]:
        """Get cached embeddings or generate new ones.

        Main entry point: deduplicates texts, checks cache, calls embed_fn
        only for uncached texts, caches results, returns in original order.
        """
        ...

    async def invalidate(self, text: str, model: str) -> bool:
        """Invalidate cached embedding. Returns True if key existed."""
        ...

    async def clear(self, model: str | None = None) -> int:
        """Clear cached embeddings. Returns number of entries deleted."""
        ...

    async def health_check(self) -> bool:
        """Check if cache backend is healthy."""
        ...

    def get_metrics(self) -> dict:
        """Get cache statistics (hits, misses, errors, hit_rate, etc.)."""
        ...
