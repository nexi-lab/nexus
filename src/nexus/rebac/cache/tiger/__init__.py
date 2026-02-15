"""Tiger Cache - Pre-materialized Permissions as Roaring Bitmaps.

Implements pre-computed permission caches for O(1) list operations,
based on SpiceDB's Tiger Cache proposal.

Submodules:
- resource_map: Maps resource UUIDs to int64 IDs for bitmap storage
- bitmap_cache: Main cache with check/update/invalidation logic
- updater: Background worker for incremental updates via changelog
- expander: Directory grant expansion worker (Leopard-style)

Related: Issue #682
"""

from nexus.rebac.cache.tiger.bitmap_cache import CacheKey, TigerCache
from nexus.rebac.cache.tiger.expander import DirectoryGrantExpander
from nexus.rebac.cache.tiger.facade import TigerFacade
from nexus.rebac.cache.tiger.resource_map import TigerResourceMap
from nexus.rebac.cache.tiger.updater import TigerCacheUpdater

__all__ = [
    "CacheKey",
    "DirectoryGrantExpander",
    "TigerCache",
    "TigerCacheUpdater",
    "TigerFacade",
    "TigerResourceMap",
]
