"""Backward-compat shim — canonical: nexus.rebac.cache.tiger.resource_map.

Deprecated: import from nexus.rebac.cache.tiger.resource_map instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.cache.tiger.resource_map is deprecated. "
    "Import from nexus.rebac.cache.tiger.resource_map instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.cache.tiger.resource_map import TigerResourceMap  # noqa: F401, E402

__all__ = ["TigerResourceMap"]
