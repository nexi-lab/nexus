"""Backward-compat shim — canonical: nexus.rebac.cache.tiger.updater.

Deprecated: import from nexus.rebac.cache.tiger.updater instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.cache.tiger.updater is deprecated. "
    "Import from nexus.rebac.cache.tiger.updater instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.cache.tiger.updater import TigerCacheUpdater  # noqa: F401, E402

__all__ = ["TigerCacheUpdater"]
