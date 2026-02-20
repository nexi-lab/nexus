"""Backward-compatibility re-export — canonical home is nexus.bricks.cache.cache_store.

.. deprecated:: Issue #2055
    Import from ``nexus.bricks.cache.cache_store`` instead of ``nexus.core.cache_store``.

The CacheStoreABC pillar follows the same placement pattern as other non-kernel pillars:
- ObjectStoreABC (Backend) lives in ``nexus.backends.backend``
- RecordStoreABC lives in ``nexus.storage.record_store``
- CacheStoreABC lives in ``nexus.bricks.cache.cache_store``

Only MetastoreABC stays in ``nexus.core`` (required at boot, kernel-level).
"""

from nexus.bricks.cache.cache_store import CacheStoreABC, NullCacheStore

__all__ = ["CacheStoreABC", "NullCacheStore"]
