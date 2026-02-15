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

from nexus.services.permissions.cache.boundary import PermissionBoundaryCache
from nexus.services.permissions.cache.coordinator import CacheCoordinator
from nexus.services.permissions.cache.iterator import (
    CachedResult,
    CursorExpiredError,
    IteratorCache,
)
from nexus.services.permissions.cache.leopard import (
    ClosureEntry,
    LeopardCache,
    LeopardIndex,
)
from nexus.services.permissions.cache.result_cache import ReBACPermissionCache
from nexus.services.permissions.cache.tiger import (
    CacheKey,
    DirectoryGrantExpander,
    TigerCache,
    TigerCacheUpdater,
    TigerResourceMap,
)
from nexus.services.permissions.cache.visibility import (
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
