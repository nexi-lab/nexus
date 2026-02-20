"""Backward-compat shim — canonical: nexus.rebac.cache.enforcer_cache.

Deprecated: import from nexus.rebac.cache.enforcer_cache instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.permission_cache is deprecated. "
    "Import from nexus.rebac.cache.enforcer_cache instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.cache.enforcer_cache import PermissionCacheCoordinator  # noqa: F401, E402

__all__ = ["PermissionCacheCoordinator"]
