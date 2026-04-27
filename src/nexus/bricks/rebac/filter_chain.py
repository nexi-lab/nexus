"""Compatibility exports for the active permission filter chain.

The runtime implementation lives in :mod:`nexus.bricks.rebac.permission_filter_chain`.
Keep this module as a stable import path so older callers do not drift onto a
duplicate implementation.
"""

from nexus.bricks.rebac.permission_filter_chain import (
    DEFAULT_FILTER_CHAIN,
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
    "DEFAULT_FILTER_CHAIN",
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
