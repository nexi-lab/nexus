"""Backward-compat shim — canonical: nexus.rebac.permissions_enhanced.

Deprecated: import from nexus.rebac.permissions_enhanced instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.permissions_enhanced is deprecated. "
    "Import from nexus.rebac.permissions_enhanced instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.permissions_enhanced import (  # noqa: F401, E402
    AdminCapability,
    AuditLogEntry,
    AuditStore,
)

__all__ = ["AdminCapability", "AuditLogEntry", "AuditStore"]
