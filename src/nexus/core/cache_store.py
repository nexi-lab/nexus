"""Backwards-compatible re-export — canonical location is nexus.cache.cache_store.

This file remains so that existing code importing from ``nexus.core.cache_store``
continues to work during transition.  New code should import directly::

    from nexus.cache.cache_store import CacheStoreABC, NullCacheStore
"""

from nexus.cache.cache_store import CacheStoreABC, NullCacheStore

__all__ = ["CacheStoreABC", "NullCacheStore"]
