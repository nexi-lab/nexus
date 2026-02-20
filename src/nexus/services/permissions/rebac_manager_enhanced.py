"""Backward-compat shim — canonical: nexus.rebac.manager.

Deprecated: import from nexus.rebac.manager instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.rebac_manager_enhanced is deprecated. "
    "Import from nexus.rebac.manager instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.manager import (  # noqa: F401, E402
    EnhancedReBACManager,
    ReBACManager,
)

__all__ = ["EnhancedReBACManager", "ReBACManager"]
