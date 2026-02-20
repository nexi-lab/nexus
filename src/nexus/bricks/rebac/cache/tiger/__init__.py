"""Tiger Cache - Pre-materialized Permissions as Roaring Bitmaps.

Canonical implementation (ORM-based, Issue #2071, #2179).

Related: Issue #682, #2071, #2179
"""

from nexus.bricks.rebac.cache.tiger.bitmap_cache import CacheKey, TigerCache
from nexus.bricks.rebac.cache.tiger.expander import DirectoryGrantExpander
from nexus.bricks.rebac.cache.tiger.facade import TigerFacade
from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap
from nexus.bricks.rebac.cache.tiger.updater import TigerCacheUpdater

__all__ = [
    "CacheKey",
    "DirectoryGrantExpander",
    "TigerCache",
    "TigerCacheUpdater",
    "TigerFacade",
    "TigerResourceMap",
]
