"""Backward-compat shim — canonical: nexus.rebac.manager.

Deprecated: import from nexus.rebac.manager instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.rebac_manager is deprecated. "
    "Import from nexus.rebac.manager instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.manager import ReBACManager  # noqa: F401, E402

__all__ = ["ReBACManager"]
