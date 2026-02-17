"""Backward-compat shim: nexus.services.permissions.permissions_enhanced.

Canonical location: ``nexus.rebac.permissions_enhanced``
"""

from nexus.rebac.permissions_enhanced import (
    AdminCapability,
    AuditLogEntry,
    AuditStore,
    EnhancedOperationContext,
)

__all__ = [
    "AdminCapability",
    "AuditLogEntry",
    "AuditStore",
    "EnhancedOperationContext",
    "EnhancedPermissionEnforcer",
]


def __getattr__(name: str):
    if name == "EnhancedPermissionEnforcer":
        from nexus.rebac.permissions_enhanced import EnhancedPermissionEnforcer
        return EnhancedPermissionEnforcer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
