"""Backward-compat shim: nexus.services.permissions.async_permissions.

Canonical location: ``nexus.rebac.async_permissions``
"""

from nexus.rebac.async_permissions import AsyncPermissionEnforcer

__all__ = ["AsyncPermissionEnforcer"]
