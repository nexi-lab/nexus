"""Backward-compat shim: nexus.services.permissions.namespace_manager.

Canonical location: ``nexus.rebac.namespace_manager``
"""

from nexus.rebac.namespace_manager import (
    MountEntry,
    NamespaceManager,
    build_mount_entries,
)

__all__ = [
    "MountEntry",
    "NamespaceManager",
    "build_mount_entries",
]
