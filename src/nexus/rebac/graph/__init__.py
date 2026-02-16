"""Permission graph computation engine.

Contains the Zanzibar-style graph traversal and expand algorithms:
- PermissionComputer: Core permission check via graph traversal
- ExpandEngine: Find all subjects with access to an object
- bulk_evaluator: In-memory graph traversal for batch checks
- ZoneAwareTraversal: Zone-scoped graph traversal with P0-5 limits

Related: Issue #1459 (decomposition)
"""

from nexus.rebac.graph.bulk_evaluator import (
    check_direct_relation,
    compute_permission,
    find_related_objects,
    find_subjects,
)
from nexus.rebac.graph.expand import ExpandEngine
from nexus.rebac.graph.traversal import PermissionComputer
from nexus.rebac.graph.zone_traversal import ZoneAwareTraversal

__all__ = [
    "ExpandEngine",
    "PermissionComputer",
    "ZoneAwareTraversal",
    "check_direct_relation",
    "compute_permission",
    "find_related_objects",
    "find_subjects",
]
