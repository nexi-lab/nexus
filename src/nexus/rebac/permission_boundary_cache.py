"""Permission Boundary Cache - Backward compatibility shim.

This module re-exports PermissionBoundaryCache from its new location
in the cache/ subpackage. All existing imports will continue to work.

New code should import from:
    nexus.rebac.cache.boundary

Related: Issue #922, Issue #1459 (decomposition)
"""

from nexus.rebac.cache.boundary import PermissionBoundaryCache  # noqa: F401

__all__ = ["PermissionBoundaryCache"]
