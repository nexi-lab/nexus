"""Backward-compat shim: nexus.services.permissions.permissions_enhanced.

Canonical location: ``nexus.rebac.permissions_enhanced``
"""

from nexus.rebac.permissions_enhanced import (
    AdminCapability,
    AuditLogEntry,
    AuditStore,
    EnhancedOperationContext,
    EnhancedPermissionEnforcer,
)

__all__ = [
    "AdminCapability",
    "AuditLogEntry",
    "AuditStore",
    "EnhancedOperationContext",
    "EnhancedPermissionEnforcer",
]
