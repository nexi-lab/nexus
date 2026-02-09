"""Nexus Cache Layer - Pluggable caching backends for permissions and metadata.

This module provides a unified caching interface that supports multiple backends:
- Dragonfly (Redis-compatible, recommended for production)
- PostgreSQL (fallback, uses existing rebac_check_cache table)

Configuration:
    Set NEXUS_DRAGONFLY_URL to enable Dragonfly backend:

    NEXUS_DRAGONFLY_URL=redis://localhost:6379

    If not set, falls back to PostgreSQL-based caching.

Usage:
    from nexus.core.cache import get_permission_cache, get_tiger_cache

    cache = get_permission_cache()
    result = await cache.get(subject_type, subject_id, permission, ...)

Embedding Cache (Issue #950):
    from nexus.core.cache import DragonflyEmbeddingCache

    # Get via factory
    embedding_cache = cache_factory.get_embedding_cache()
    if embedding_cache:
        embeddings = await embedding_cache.get_or_embed_batch(texts, model, embed_fn)
"""

from nexus.core.cache.base import PermissionCacheProtocol, TigerCacheProtocol
from nexus.core.cache.dragonfly import DragonflyCacheStore, DragonflyEmbeddingCache
from nexus.core.cache.factory import CacheFactory
from nexus.core.cache.inmemory import InMemoryCacheStore
from nexus.core.cache.settings import CacheSettings

__all__ = [
    "CacheSettings",
    "CacheFactory",
    "DragonflyCacheStore",
    "DragonflyEmbeddingCache",
    "InMemoryCacheStore",
    "PermissionCacheProtocol",
    "TigerCacheProtocol",
]
