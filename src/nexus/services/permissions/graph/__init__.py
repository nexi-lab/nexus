"""Permission graph computation engine.

Contains the Zanzibar-style graph traversal and expand algorithms:
- PermissionComputer: Core permission check via graph traversal
- ExpandEngine: Find all subjects with access to an object
- bulk_evaluator: In-memory graph traversal for batch checks

Related: Issue #1459 (decomposition)
"""

from nexus.services.permissions.graph.bulk_evaluator import (
    check_direct_relation,
    compute_permission,
    find_related_objects,
    find_subjects,
)
from nexus.services.permissions.graph.expand import ExpandEngine
from nexus.services.permissions.graph.traversal import PermissionComputer

__all__ = [
    "ExpandEngine",
    "PermissionComputer",
    "check_direct_relation",
    "compute_permission",
    "find_related_objects",
    "find_subjects",
]
