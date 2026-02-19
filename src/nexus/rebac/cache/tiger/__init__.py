"""Tiger Cache - Pre-materialized Permissions as Roaring Bitmaps.

Consolidated: canonical implementation lives in
``nexus.services.permissions.cache.tiger`` (ORM-based, Issue #2071).
This module re-exports for backward compatibility.

Related: Issue #682, #2071
"""

from nexus.services.permissions.cache.tiger.bitmap_cache import CacheKey, TigerCache
from nexus.services.permissions.cache.tiger.expander import DirectoryGrantExpander
from nexus.services.permissions.cache.tiger.facade import TigerFacade
from nexus.services.permissions.cache.tiger.resource_map import TigerResourceMap
from nexus.services.permissions.cache.tiger.updater import TigerCacheUpdater

__all__ = [
    "CacheKey",
    "DirectoryGrantExpander",
    "TigerCache",
    "TigerCacheUpdater",
    "TigerFacade",
    "TigerResourceMap",
]
