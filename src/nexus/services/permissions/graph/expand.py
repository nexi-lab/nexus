"""Backward-compat shim — canonical: nexus.rebac.graph.expand.

Deprecated: import from nexus.rebac.graph.expand instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.graph.expand is deprecated. "
    "Import from nexus.rebac.graph.expand instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.graph.expand import ExpandEngine  # noqa: F401, E402

__all__ = ["ExpandEngine"]
