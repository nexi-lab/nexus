"""Backward-compat shim — canonical: nexus.rebac.cache.tiger.

Deprecated: import from nexus.rebac.cache.tiger instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.cache.tiger is deprecated. "
    "Import from nexus.rebac.cache.tiger instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.cache.tiger import (  # noqa: F401, E402
    CacheKey,
    DirectoryGrantExpander,
    TigerCache,
    TigerCacheUpdater,
    TigerFacade,
    TigerResourceMap,
)

__all__ = [
    "CacheKey",
    "DirectoryGrantExpander",
    "TigerCache",
    "TigerCacheUpdater",
    "TigerFacade",
    "TigerResourceMap",
]
