"""Directory Visibility Cache - Backward compatibility shim.

This module re-exports DirectoryVisibilityCache and VisibilityEntry
from their new location in the cache/ subpackage. All existing imports
will continue to work.

New code should import from:
    nexus.rebac.cache.visibility

Related: Issue #919, Issue #1459 (decomposition)
"""

from nexus.rebac.cache.visibility import (  # noqa: F401
    DirectoryVisibilityCache,
    VisibilityEntry,
)

__all__ = ["DirectoryVisibilityCache", "VisibilityEntry"]
