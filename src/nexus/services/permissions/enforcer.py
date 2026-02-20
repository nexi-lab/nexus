"""Backward-compat shim — canonical: nexus.rebac.enforcer.

Deprecated: import from nexus.rebac.enforcer instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.enforcer is deprecated. Import from nexus.rebac.enforcer instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.enforcer import PermissionEnforcer  # noqa: F401, E402

__all__ = ["PermissionEnforcer"]
