"""Backward-compat shim — canonical: nexus.rebac.utils.fast.

Deprecated: import from nexus.rebac.utils.fast instead.
"""

import warnings

warnings.warn(
    "nexus.services.permissions.utils.fast is deprecated. "
    "Import from nexus.rebac.utils.fast instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nexus.rebac.utils.fast import (  # noqa: F401, E402
    check_permission_single_rust,
    check_permission_single_with_fallback,
    check_permissions_bulk_rust,
    check_permissions_bulk_with_fallback,
    estimate_speedup,
    expand_subjects_rust,
    expand_subjects_with_fallback,
    get_performance_stats,
    is_rust_available,
    list_objects_for_subject_rust,
    list_objects_for_subject_with_fallback,
)

__all__ = [
    "check_permission_single_rust",
    "check_permission_single_with_fallback",
    "check_permissions_bulk_rust",
    "check_permissions_bulk_with_fallback",
    "estimate_speedup",
    "expand_subjects_rust",
    "expand_subjects_with_fallback",
    "get_performance_stats",
    "is_rust_available",
    "list_objects_for_subject_rust",
    "list_objects_for_subject_with_fallback",
]
