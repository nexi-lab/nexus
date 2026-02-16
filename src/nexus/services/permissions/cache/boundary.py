"""Permission Boundary Cache - Backward compatibility shim.

Re-exports from nexus.rebac.cache.boundary.
New code should import from nexus.rebac.cache.boundary.
"""

from nexus.rebac.cache.boundary import PermissionBoundaryCache  # noqa: F401

__all__ = ["PermissionBoundaryCache"]
