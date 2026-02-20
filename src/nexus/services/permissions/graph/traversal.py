"""Backward-compat shim — canonical: nexus.rebac.graph.traversal.

Deprecated: import from nexus.rebac.graph.traversal instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.graph.traversal is deprecated. "
    "Import from nexus.rebac.graph.traversal instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.graph.traversal import PermissionComputer  # noqa: F401, E402

__all__ = ["PermissionComputer"]
