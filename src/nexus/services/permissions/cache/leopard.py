"""Leopard cache - Backward compatibility shim.

Re-exports from nexus.rebac.cache.leopard.
New code should import from nexus.rebac.cache.leopard.
"""

from nexus.rebac.cache.leopard import (  # noqa: F401
    GROUP_ENTITY_TYPES,
    MEMBERSHIP_RELATIONS,
    ClosureEntry,
    LeopardCache,
    LeopardIndex,
)

__all__ = [
    "GROUP_ENTITY_TYPES",
    "MEMBERSHIP_RELATIONS",
    "ClosureEntry",
    "LeopardCache",
    "LeopardIndex",
]
