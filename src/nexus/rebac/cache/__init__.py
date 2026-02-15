"""Cache subsystem for ReBAC permissions.

Contains multi-layer caching infrastructure:
- Tiger Cache: Pre-materialized permissions as Roaring Bitmaps
- Result Cache: L1 in-memory permission check cache
- Boundary Cache: O(1) inheritance boundary lookups
- Visibility Cache: Directory visibility caching
- Iterator Cache: Paginated query result caching
- Leopard Index: Transitive group closure index

Related: Issue #1459 (decomposition)
"""

from nexus.rebac.cache.boundary import PermissionBoundaryCache
from nexus.rebac.cache.coordinator import CacheCoordinator
from nexus.rebac.cache.iterator import (
    CachedResult,
    CursorExpiredError,
    IteratorCache,
)
from nexus.rebac.cache.leopard import (
    ClosureEntry,
    LeopardCache,
    LeopardIndex,
)
from nexus.rebac.cache.result_cache import ReBACPermissionCache
from nexus.rebac.cache.tiger import (
    CacheKey,
    DirectoryGrantExpander,
    TigerCache,
    TigerCacheUpdater,
    TigerResourceMap,
)
from nexus.rebac.cache.visibility import (
    DirectoryVisibilityCache,
    VisibilityEntry,
)

__all__ = [
    # Result cache (L1)
    "ReBACPermissionCache",
    # Boundary cache
    "PermissionBoundaryCache",
    # Coordinator
    "CacheCoordinator",
    # Visibility cache
    "DirectoryVisibilityCache",
    "VisibilityEntry",
    # Iterator cache
    "CachedResult",
    "CursorExpiredError",
    "IteratorCache",
    # Leopard index
    "ClosureEntry",
    "LeopardCache",
    "LeopardIndex",
    # Tiger cache
    "CacheKey",
    "DirectoryGrantExpander",
    "TigerCache",
    "TigerCacheUpdater",
    "TigerResourceMap",
]
