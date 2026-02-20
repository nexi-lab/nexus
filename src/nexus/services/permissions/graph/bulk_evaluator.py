"""Backward-compat shim — canonical: nexus.rebac.graph.bulk_evaluator.

Deprecated: import from nexus.rebac.graph.bulk_evaluator instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.graph.bulk_evaluator is deprecated. "
    "Import from nexus.rebac.graph.bulk_evaluator instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.graph.bulk_evaluator import (  # noqa: F401, E402
    check_direct_relation,
    compute_permission,
    find_related_objects,
    find_subjects,
)

__all__ = [
    "check_direct_relation",
    "compute_permission",
    "find_related_objects",
    "find_subjects",
]
