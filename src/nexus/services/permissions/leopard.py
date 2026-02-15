"""Leopard Index - Backward compatibility shim.

This module re-exports LeopardCache, LeopardIndex, and ClosureEntry
from their new location in the cache/ subpackage. All existing imports
will continue to work.

New code should import from:
    nexus.services.permissions.cache.leopard

Related: Issue #692, Issue #1459 (decomposition)
"""

from nexus.services.permissions.cache.leopard import (  # noqa: F401
    GROUP_ENTITY_TYPES,
    MEMBERSHIP_RELATIONS,
    ClosureEntry,
    LeopardCache,
    LeopardIndex,
)

__all__ = [
    "ClosureEntry",
    "GROUP_ENTITY_TYPES",
    "LeopardCache",
    "LeopardIndex",
    "MEMBERSHIP_RELATIONS",
]
