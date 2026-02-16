"""Backward-compat shim — canonical location is ``nexus.rebac.types`` (Issue #1385)."""

from nexus.rebac.types import (  # noqa: F401
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
    "ConsistencyLevel",
    "ConsistencyMode",
    "ConsistencyRequirement",
    "WriteResult",
    "CheckResult",
    "TraversalStats",
    "GraphLimits",
    "GraphLimitExceeded",
]
