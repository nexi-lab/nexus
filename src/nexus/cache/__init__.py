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

Note:
    DragonflyCacheStore is lazily imported to avoid pulling in the ``redis``
    package at import time. Access it via::

        from nexus.cache import DragonflyCacheStore
        # or
        from nexus.cache.dragonfly import DragonflyCacheStore
"""

from nexus.cache.base import (
    EmbeddingCacheProtocol,
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.cache.brick import CacheBrick
from nexus.cache.factory import CacheFactory
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.cache.settings import CacheSettings
from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore

__all__ = [
    # Brick facade (Issue #1524)
    "CacheBrick",
    # Factory + config (deprecated — use CacheBrick instead)
    "CacheFactory",
    "CacheSettings",
    # Consumer-facing protocols (what you program against)
    "EmbeddingCacheProtocol",
    "PermissionCacheProtocol",
    "ResourceMapCacheProtocol",
    "TigerCacheProtocol",
    # Fourth Pillar ABC — canonical home is nexus.contracts.cache_store (Issue #2055)
    "CacheStoreABC",
    "NullCacheStore",
    # CacheStoreABC drivers (for DI into CacheBrick/NexusFS)
    # DragonflyCacheStore — lazy import (use: from nexus.cache.dragonfly import ...)
    "InMemoryCacheStore",
]


def __getattr__(name: str) -> object:
    """Lazy import for DragonflyCacheStore to avoid pulling in redis at import time."""
    if name == "DragonflyCacheStore":
        from nexus.cache.dragonfly import DragonflyCacheStore

        globals()["DragonflyCacheStore"] = DragonflyCacheStore
        return DragonflyCacheStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
