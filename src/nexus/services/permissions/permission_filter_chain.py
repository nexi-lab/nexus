"""Backward-compat shim — canonical: nexus.rebac.filter_chain.

Deprecated: import from nexus.rebac.filter_chain instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.permission_filter_chain is deprecated. "
    "Import from nexus.rebac.filter_chain instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.filter_chain import (  # noqa: F401, E402
    BulkReBACStrategy,
    FilterContext,
    FilterResult,
    FilterStrategy,
    HierarchyPreFilterStrategy,
    LeopardIndexStrategy,
    TigerBitmapStrategy,
    ZonePreFilterStrategy,
    run_filter_chain,
)

__all__ = [
    "BulkReBACStrategy",
    "FilterContext",
    "FilterResult",
    "FilterStrategy",
    "HierarchyPreFilterStrategy",
    "LeopardIndexStrategy",
    "TigerBitmapStrategy",
    "ZonePreFilterStrategy",
    "run_filter_chain",
]
