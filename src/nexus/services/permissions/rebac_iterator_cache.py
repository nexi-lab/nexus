"""Iterator Cache - Backward compatibility shim.

This module re-exports IteratorCache, CachedResult, and CursorExpiredError
from their new location in the cache/ subpackage. All existing imports
will continue to work.

New code should import from:
    nexus.services.permissions.cache.iterator

Related: Issue #1459 (decomposition)
"""

from nexus.services.permissions.cache.iterator import (  # noqa: F401
    CachedResult,
    CursorExpiredError,
    IteratorCache,
)

__all__ = ["CachedResult", "CursorExpiredError", "IteratorCache"]
