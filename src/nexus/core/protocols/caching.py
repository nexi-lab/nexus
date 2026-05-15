"""Caching protocol contracts (Issue #1628, #2362, #2364).

Caching contracts:

- ``CacheConfigContract`` — the 3-attribute contract for cache configuration
  (session_factory, zone_id, l1_only).

- ``TigerCacheProtocol`` — protocol for Tiger bitmap cache backends (cross-brick).

- ``EmbeddingCacheProtocol`` — protocol for embedding vector caches (cross-brick).

References:
    - Issue #1628: Cache protocol contracts
    - Issue #2362: ConnectorProtocol wrapping chains
    - Issue #2364: Consolidate duplicate top-level modules
    - docs/design/cache-layer.md
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CacheConfigContract(Protocol):
    """Contract for connectors that support caching configuration.

    Documents the attributes that cache-aware components expect from a
    connector reference.

    Attributes:
        session_factory: SQLAlchemy session factory (None for L1-only mode)
        zone_id: Cache zone identifier (defaults to "root")
        l1_only: If True, skip L2 (disk/DB) caching entirely
    """

    session_factory: Any | None
    zone_id: str | None
    l1_only: bool

@runtime_checkable
class TigerCacheProtocol(Protocol):
    """Protocol for Tiger cache backends (cross-brick contract).

    Tiger cache stores pre-materialized permission bitmaps for O(1) list filtering.
    Each bitmap represents all resources a subject can access with a given permission.

    Canonical implementation lives in ``nexus.cache``; this protocol is
    defined here so that ``nexus.bricks.rebac`` can type-reference it without
    a cross-brick import.
    """

    async def get_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> tuple[bytes, int] | None:
        """Get Tiger bitmap for a subject."""
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
        """Store Tiger bitmap for a subject."""
        ...

    async def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate Tiger cache entries matching criteria."""
        ...

    async def health_check(self) -> bool:
        """Check if the cache backend is healthy."""
        ...

@runtime_checkable
class EmbeddingCacheProtocol(Protocol):
    """Protocol for embedding vector caches (cross-brick contract).

    Caches embedding vectors by content hash to avoid redundant API calls.
    Supports batch operations with deduplication for efficiency.

    Canonical implementation lives in ``nexus.cache``; this protocol is
    defined here so that ``nexus.bricks.search`` can type-reference it without
    a cross-brick import.
    """

    async def get(self, text: str, model: str) -> list[float] | None:
        """Get cached embedding for text."""
        ...

    async def set(self, text: str, model: str, embedding: list[float]) -> None:
        """Cache embedding for text."""
        ...

    async def get_batch(self, texts: list[str], model: str) -> dict[str, list[float] | None]:
        """Get cached embeddings for multiple texts."""
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
        """Get cached embeddings or generate new ones."""
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

    def get_metrics(self) -> dict[str, Any]:
        """Get cache statistics (hits, misses, errors, hit_rate, etc.)."""
        ...
