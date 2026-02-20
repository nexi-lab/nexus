"""Backward-compat shim — canonical: nexus.rebac.consistency.

Deprecated: import from nexus.rebac.consistency instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.consistency is deprecated. "
    "Import from nexus.rebac.consistency instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.consistency import (  # noqa: F401, E402
    ZoneIsolationError,
    ZoneManager,
    get_zone_revision_for_grant,
    increment_version_token,
)

__all__ = [
    "ZoneIsolationError",
    "ZoneManager",
    "get_zone_revision_for_grant",
    "increment_version_token",
]
