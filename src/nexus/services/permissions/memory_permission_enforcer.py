"""Backward-compat shim: nexus.services.permissions.memory_permission_enforcer.

Canonical location: ``nexus.rebac.memory_permission_enforcer``
"""

from nexus.rebac.memory_permission_enforcer import MemoryPermissionEnforcer

__all__ = ["MemoryPermissionEnforcer"]
