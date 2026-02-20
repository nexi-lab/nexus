"""Shared type definitions for the ReBAC brick (Issue #1385).

Backward-compat shim (Issue #2190): Canonical location is
``nexus.contracts.rebac_types``. This module re-exports for existing importers.

Backward-compat shim: ``nexus.services.permissions.types``
"""

from nexus.contracts.rebac_types import CheckResult as CheckResult  # noqa: F401
from nexus.contracts.rebac_types import ConsistencyLevel as ConsistencyLevel  # noqa: F401
from nexus.contracts.rebac_types import ConsistencyMode as ConsistencyMode  # noqa: F401
from nexus.contracts.rebac_types import (  # noqa: F401
    ConsistencyRequirement as ConsistencyRequirement,
)
from nexus.contracts.rebac_types import GraphLimitExceeded as GraphLimitExceeded  # noqa: F401
from nexus.contracts.rebac_types import GraphLimits as GraphLimits  # noqa: F401
from nexus.contracts.rebac_types import TraversalStats as TraversalStats  # noqa: F401
from nexus.contracts.rebac_types import WriteResult as WriteResult  # noqa: F401

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
