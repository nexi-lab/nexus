"""Backward-compat shim: nexus.services.permissions.deferred_permission_buffer.

Canonical location: ``nexus.rebac.deferred_permission_buffer``
"""

from nexus.rebac.deferred_permission_buffer import (
    DeferredPermissionBuffer,
    get_default_buffer,
    set_default_buffer,
)

__all__ = [
    "DeferredPermissionBuffer",
    "get_default_buffer",
    "set_default_buffer",
]
