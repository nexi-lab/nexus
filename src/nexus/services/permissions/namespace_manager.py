"""Backward-compat shim — canonical: nexus.rebac.namespace_manager.

Deprecated: import from nexus.rebac.namespace_manager instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.namespace_manager is deprecated. "
    "Import from nexus.rebac.namespace_manager instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.namespace_manager import (  # noqa: F401, E402
    MountEntry,
    NamespaceManager,
    NamespaceMount,
    build_mount_entries,
)

__all__ = ["MountEntry", "NamespaceManager", "NamespaceMount", "build_mount_entries"]
