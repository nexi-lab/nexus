"""Backward-compat shim — canonical: nexus.rebac.cache.tiger.bitmap_cache.

Deprecated: import from nexus.rebac.cache.tiger.bitmap_cache instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.cache.tiger.bitmap_cache is deprecated. "
    "Import from nexus.rebac.cache.tiger.bitmap_cache instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.cache.tiger.bitmap_cache import CacheKey, TigerCache  # noqa: F401, E402

__all__ = ["CacheKey", "TigerCache"]
