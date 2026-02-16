"""Backward-compatibility shim — canonical module is ``utils.fast``.

All public symbols are re-exported so existing ``from
nexus.rebac.rebac_fast import ...`` continues to work.
"""

from nexus.rebac.utils.fast import (  # noqa: F401
    RUST_AVAILABLE,
    NamespaceConfigDict,
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
