"""ReBAC Permission Cache - Backward compatibility shim.

This module re-exports ReBACPermissionCache from its new location
in the cache/ subpackage. All existing imports will continue to work.

New code should import from:
    nexus.rebac.cache.result_cache

Related: Issue #1077, Issue #1459 (decomposition)
"""

from nexus.rebac.cache.result_cache import ReBACPermissionCache  # noqa: F401

__all__ = ["ReBACPermissionCache"]
