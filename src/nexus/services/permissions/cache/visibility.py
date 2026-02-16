"""Directory Visibility Cache - Backward compatibility shim.

Re-exports from nexus.rebac.cache.visibility.
New code should import from nexus.rebac.cache.visibility.
"""

from nexus.rebac.cache.visibility import (  # noqa: F401
    DirectoryVisibilityCache,
    VisibilityEntry,
)

__all__ = ["DirectoryVisibilityCache", "VisibilityEntry"]
