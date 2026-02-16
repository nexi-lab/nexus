"""Tiger Cache - Backward compatibility shim.

This module re-exports all Tiger Cache classes from their new locations
in the cache/tiger/ subpackage. All existing imports from this module
will continue to work.

New code should import from:
    nexus.rebac.cache.tiger

Related: Issue #682, Issue #1459 (decomposition)
"""

from nexus.rebac.cache.tiger.bitmap_cache import (  # noqa: F401
    CacheKey,
    TigerCache,
)
from nexus.rebac.cache.tiger.expander import (  # noqa: F401
    DirectoryGrantExpander,
)
from nexus.rebac.cache.tiger.resource_map import (  # noqa: F401
    TigerResourceMap,
)
from nexus.rebac.cache.tiger.updater import (  # noqa: F401
    TigerCacheUpdater,
)

__all__ = [
    "CacheKey",
    "DirectoryGrantExpander",
    "TigerCache",
    "TigerCacheUpdater",
    "TigerResourceMap",
]
