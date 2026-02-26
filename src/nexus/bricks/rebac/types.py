"""Backward-compat shim — canonical home is nexus.contracts.rebac_types."""

from nexus.contracts.rebac_types import (
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
    "CheckResult",
    "ConsistencyLevel",
    "ConsistencyMode",
    "ConsistencyRequirement",
    "GraphLimitExceeded",
    "GraphLimits",
    "TraversalStats",
    "WriteResult",
]
