"""Backward-compat shim — canonical: nexus.rebac.graph.zone_traversal.

Deprecated: import from nexus.rebac.graph.zone_traversal instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.graph.zone_traversal is deprecated. "
    "Import from nexus.rebac.graph.zone_traversal instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.graph.zone_traversal import ZoneAwareTraversal  # noqa: F401, E402

__all__ = ["ZoneAwareTraversal"]
