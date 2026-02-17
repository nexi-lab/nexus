"""ReBAC permission cache - Backward compatibility shim.

Re-exports from nexus.rebac.cache.result_cache.
New code should import from nexus.rebac.cache.result_cache.
"""

from nexus.rebac.cache.result_cache import ReBACPermissionCache  # noqa: F401

__all__ = ["ReBACPermissionCache"]
