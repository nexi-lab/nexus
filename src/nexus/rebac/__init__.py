"""ReBAC brick — Relationship-Based Access Control (Issue #1385).

Provides Zanzibar-style ReBAC as a removable Brick (tier 4):
- Core ReBACManager (flattened from Enhanced + base)
- Multi-layer caching (Tiger, Leopard, Boundary, L1)
- Graph traversal with DoS protection (P0-5)
- Zone isolation and consistency guarantees (P0-1, P0-2)

Lazy __init__.py — only protocols + types are eagerly imported.
"""

from nexus.rebac.types import (
    CheckResult,
    ConsistencyLevel,
    ConsistencyMode,
    ConsistencyRequirement,
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
    WriteResult,
)

__all__ = [
    # Types (eagerly loaded)
    "CheckResult",
    "ConsistencyLevel",
    "ConsistencyMode",
    "ConsistencyRequirement",
    "GraphLimitExceeded",
    "GraphLimits",
    "TraversalStats",
    "WriteResult",
]
