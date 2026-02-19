"""Nexus Cache Layer — Tier 2 BRICK for pluggable caching backends.

All domain caches are driver-agnostic, built on CacheStoreABC primitives.
CacheBrick is the single entry point for all cache domain services.

Configuration:
    Set NEXUS_DRAGONFLY_URL to enable Dragonfly backend:

    NEXUS_DRAGONFLY_URL=redis://localhost:6379

    If not set, NullCacheStore provides graceful degradation (Tier 2 = silent).

Usage:
    from nexus.cache import CacheBrick

    brick = CacheBrick(cache_store=my_store)
    await brick.start()

    perm_cache = brick.permission_cache
    tiger_cache = brick.tiger_cache
"""

from nexus.backends.caching_wrapper import (
    CacheStrategy,
    CacheWrapperConfig,
    CachingBackendWrapper,
)
from nexus.cache.base import (
    EmbeddingCacheProtocol,
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.cache.brick import CacheBrick
from nexus.cache.cache_store import CacheStoreABC, NullCacheStore
from nexus.cache.dragonfly import DragonflyCacheStore
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.cache.settings import CacheSettings

__all__ = [
    # Brick facade (Issue #1524)
    "CacheBrick",
    # Configuration
    "CacheSettings",
    # CacheStoreABC pillar (canonical location)
    "CacheStoreABC",
    "NullCacheStore",
    # CachingBackendWrapper — transparent caching decorator for any Backend (#1392)
    "CachingBackendWrapper",
    "CacheStrategy",
    "CacheWrapperConfig",
    # Consumer-facing protocols (what you program against)
    "EmbeddingCacheProtocol",
    "PermissionCacheProtocol",
    "ResourceMapCacheProtocol",
    "TigerCacheProtocol",
    # CacheStoreABC drivers (for DI into CacheBrick/NexusFS)
    "DragonflyCacheStore",
    "InMemoryCacheStore",
]
