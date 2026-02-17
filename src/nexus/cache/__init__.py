"""Nexus Cache Layer - Pluggable caching backends for permissions and metadata.

All domain caches are driver-agnostic, built on CacheStoreABC primitives.
CacheFactory (systemd layer) creates them; kernel never imports directly.

Configuration:
    Set NEXUS_DRAGONFLY_URL to enable Dragonfly backend:

    NEXUS_DRAGONFLY_URL=redis://localhost:6379

    If not set, NullCacheStore provides graceful degradation.

Usage:
    from nexus.cache import get_permission_cache, get_tiger_cache

    cache = get_permission_cache()
    result = await cache.get(subject_type, subject_id, permission, ...)

    embedding_cache = cache_factory.get_embedding_cache()
    embeddings = await embedding_cache.get_or_embed_batch(texts, model, embed_fn)
"""

from nexus.cache.base import (
    EmbeddingCacheProtocol,
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.cache.dragonfly import DragonflyCacheStore
from nexus.cache.factory import CacheFactory
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.cache.settings import CacheSettings

__all__ = [
    # Factory + config
    "CacheFactory",
    "CacheSettings",
    # Consumer-facing protocols (what you program against)
    "EmbeddingCacheProtocol",
    "PermissionCacheProtocol",
    "ResourceMapCacheProtocol",
    "TigerCacheProtocol",
    # CacheStoreABC drivers (for DI into CacheFactory/NexusFS)
    "DragonflyCacheStore",
    "InMemoryCacheStore",
]
