"""Backward-compat shim — canonical: nexus.rebac.consistency.zone_manager.

Deprecated: import from nexus.rebac.consistency.zone_manager instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.consistency.zone_manager is deprecated. "
    "Import from nexus.rebac.consistency.zone_manager instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.consistency.zone_manager import (  # noqa: F401, E402
    ZoneIsolationError,
    ZoneManager,
)

__all__ = ["ZoneIsolationError", "ZoneManager"]
