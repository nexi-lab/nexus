"""
Fast ReBAC permission checking with Rust acceleration.

This module provides a drop-in replacement for Python-based permission checking
with significant performance improvements for bulk operations.

Performance characteristics:
- Single check: ~50x speedup (but Python overhead may dominate)
- 10-100 checks: ~70-80x speedup
- 1000+ checks: ~85x speedup (~6µs per check vs ~500µs in Python)

The module automatically falls back to Python implementation if Rust is unavailable.
"""

import logging
from typing import TYPE_CHECKING, Any

# RUST_FALLBACK: rebac — optional Rust symbols are routed through
# nexus._rust_compat so absent/stale binaries fall back at call time
# instead of failing during module import.
from nexus._rust_compat import compute_permission_single as _compute_permission_single
from nexus._rust_compat import compute_permissions_bulk as _compute_permissions_bulk
from nexus._rust_compat import expand_subjects as _expand_subjects
from nexus._rust_compat import list_objects_for_subject as _list_objects_for_subject

if TYPE_CHECKING:
    from nexus.bricks.rebac.domain import Entity
    from nexus.bricks.rebac.domain import NamespaceConfig as ReBACNamespaceConfig

# Internal type for namespace config dict (not the NamespaceConfig class)
NamespaceConfigDict = dict[str, Any]  # Contains 'relations' and 'permissions' keys

logger = logging.getLogger(__name__)

RUST_AVAILABLE = _compute_permissions_bulk is not None
if RUST_AVAILABLE:
    logger.info("Rust acceleration available (nexus_runtime)")
else:
    logger.debug("Rust acceleration unavailable; using Python ReBAC fallback")


def is_rust_available() -> bool:
    """Check if Rust acceleration is available.

    Returns:
        True if nexus_runtime Rust extension is loaded, False otherwise
    """
    return RUST_AVAILABLE


def check_permissions_bulk_rust(
    checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
    tuple_version: int = 0,
) -> dict[tuple[str, str, str, str, str], bool]:
    """
    Check multiple permissions using Rust implementation.

    This is the low-level interface to the Rust extension. For most use cases,
    use the higher-level wrapper functions instead.

    Args:
        checks: List of (subject, permission, object) tuples where:
            - subject: (subject_type: str, subject_id: str)
            - permission: str
            - object: (object_type: str, object_id: str)

        tuples: List of ReBAC relationship dictionaries with keys:
            - subject_type: str
            - subject_id: str
            - subject_relation: Optional[str]
            - relation: str
            - object_type: str
            - object_id: str

        namespace_configs: Dict mapping object_type -> namespace config:
            {
                "object_type": {
                    "relations": {
                        "relation_name": "direct" | {"union": [...]} |
                                       {"tupleToUserset": {"tupleset": str, "computedUserset": str}}
                    },
                    "permissions": {
                        "permission_name": [userset1, userset2, ...]
                    }
                }
            }

    Returns:
        Dict mapping (subject_type, subject_id, permission, object_type, object_id) -> bool

    Raises:
        RuntimeError: If Rust extension is not available
        ValueError: If input data format is invalid
    """
    if not RUST_AVAILABLE:
        raise RuntimeError(
            "Rust acceleration not available (nexus_runtime extension not present in this build; the kernel runs as a separate process via gRPC — see _rust_compat.py)"
        )

    try:
        if _compute_permissions_bulk is None:
            raise RuntimeError("Rust bulk permission check not available")

        # Try with tuple_version first (newer API)
        try:
            result: Any = _compute_permissions_bulk(
                checks, tuples, namespace_configs, tuple_version
            )
        except TypeError as te:
            # Fallback to old API without tuple_version parameter
            if "takes 3 positional arguments" in str(te):
                logger.debug("Rust module uses old API (3 args), calling without tuple_version")
                result = _compute_permissions_bulk(checks, tuples, namespace_configs)
            else:
                raise

        return result  # allowed
    except (RuntimeError, ValueError) as e:
        logger.error(f"Rust permission check failed: {e}", exc_info=True)
        raise


def check_permissions_bulk_with_fallback(
    checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
    force_python: bool = False,
    tuple_version: int = 0,
) -> dict[tuple[str, str, str, str, str], bool]:
    """
    Check multiple permissions with automatic fallback to Python.

    This is the recommended high-level interface. It automatically uses Rust
    if available, with transparent fallback to Python implementation.

    Args:
        checks: List of (subject, permission, object) tuples
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Dict mapping object_type -> namespace config
        force_python: Force use of Python implementation (for testing/debugging)
        tuple_version: Version counter for Rust graph cache invalidation

    Returns:
        Dict mapping (subject_type, subject_id, permission, object_type, object_id) -> bool

    Example:
        >>> checks = [
        ...     (("user", "alice"), "read", ("file", "doc1")),
        ...     (("user", "bob"), "write", ("file", "doc2")),
        ... ]
        >>> tuples = [...]  # ReBAC tuples from database
        >>> configs = {...}  # Namespace configurations
        >>> results = check_permissions_bulk_with_fallback(checks, tuples, configs)
        >>> results[("user", "alice", "read", "file", "doc1")]  # True/False
    """
    if RUST_AVAILABLE and not force_python:
        try:
            import time

            start = time.perf_counter()
            result = check_permissions_bulk_rust(checks, tuples, namespace_configs, tuple_version)
            elapsed = time.perf_counter() - start
            logger.info(
                f"[RUST-INNER] Pure Rust computation: {elapsed * 1000:.1f}ms for {len(checks)} checks"
            )
            return result
        except (RuntimeError, ValueError) as e:
            logger.warning(f"Rust permission check failed, falling back to Python: {e}")
            # Fall through to Python implementation

    # Fallback: compute in Python
    logger.debug(f"Computing {len(checks)} permissions in Python")
    return _check_permissions_bulk_python(checks, tuples, namespace_configs)


def _check_permissions_bulk_python(
    checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
) -> dict[tuple[str, str, str, str, str], bool]:
    """Pure Python implementation for the Rust-fallback path.

    Round-3 review fix (codex HIGH): delegate to the full Zanzibar-style
    in-memory traversal in ``nexus.bricks.rebac.graph.bulk_evaluator``
    instead of the previous simplified expansion. The simplified path
    silently returned False for ``tupleToUserset``, ``intersection``,
    and ``exclusion`` shapes — which made the wildcard fix in #4239
    (a tuple at ``/workspaces/ws1`` granting ``/workspaces/ws1/a.md``)
    not work in Rust-free edge images, because the default file
    namespace inherits via ``parent_viewer/parent_editor tupleToUserset``.

    Round-2's ``_unwrap_userset`` helper + ``_compute_permission_simple``
    are kept ONLY as a last-ditch backstop in case bulk_evaluator can't
    be imported (e.g. partial install). The shared bulk_memo_cache gives
    the same cross-check memoization benefit round-1 introduced.
    """
    from nexus.bricks.rebac.domain import Entity, NamespaceConfig

    # Convert namespace configs to NamespaceConfig instances so the
    # bulk_evaluator's get_namespace callable returns the proper type.
    namespaces: dict[str, NamespaceConfig] = {}
    for obj_type, config_dict in namespace_configs.items():
        if isinstance(config_dict, NamespaceConfig):
            namespaces[obj_type] = config_dict
        else:
            namespaces[obj_type] = NamespaceConfig(
                namespace_id="",
                object_type=obj_type,
                config=config_dict,
            )

    def _get_namespace(obj_type: str) -> NamespaceConfig | None:
        return namespaces.get(obj_type)

    # Filter conditioned tuples — the bulk_evaluator can't evaluate
    # ABAC predicates without a context, so they must fail-closed.
    # Preserves the prior behavior covered by
    # test_python_fallback_denies_conditioned_tuple_without_context.
    eligible_tuples = [t for t in tuples if not t.get("conditions")]

    results: dict[tuple[str, str, str, str, str], bool] = {}

    try:
        from nexus.bricks.rebac.graph import bulk_evaluator
    except ImportError:
        # Degraded path: bulk_evaluator unavailable. Use the
        # round-2 simplified expansion as a backstop. This loses
        # tupleToUserset/intersection/exclusion but at least handles
        # direct + union, which is what the prior fallback did.
        return _check_permissions_bulk_python_simple(checks, eligible_tuples, namespaces)

    # Shared memo across all checks in this bulk call (round-1's
    # cross-check positive memoization benefit, preserved).
    bulk_memo: dict[tuple[str, str, str, str, str], bool] = {}

    # Round-3 review fix: zone_id is required by bulk_evaluator for tuple
    # zone filtering, but the simple-fallback callers don't carry zone
    # context; pass "" so the evaluator's zone filter is a no-op (mirrors
    # how compute_permission_zone_aware_with_limits treats missing zones).
    zone_id = ""

    for subject_tuple, permission, object_tuple in checks:
        subject = Entity(subject_tuple[0], subject_tuple[1])
        obj = Entity(object_tuple[0], object_tuple[1])

        granted = bulk_evaluator.compute_permission(
            subject=subject,
            permission=permission,
            obj=obj,
            zone_id=zone_id,
            tuples_graph=eligible_tuples,
            get_namespace=_get_namespace,
            bulk_memo_cache=bulk_memo,
        )
        key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        results[key] = bool(granted)

    return results


def _check_permissions_bulk_python_simple(
    checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    eligible_tuples: list[dict[str, Any]],
    namespaces: dict[str, Any],
) -> dict[tuple[str, str, str, str, str], bool]:
    """Round-2 simplified fallback — direct + union only.

    Kept as a backstop for the degraded case where ``bulk_evaluator``
    can't be imported. tupleToUserset/intersection/exclusion are not
    supported here (Codex round-3 HIGH).
    """
    from nexus.bricks.rebac.domain import Entity

    direct_index: set[tuple[str, str, str, str, str]] = set()
    for t in eligible_tuples:
        if t.get("subject_relation") is not None:
            continue
        direct_index.add(
            (
                t["subject_type"],
                t["subject_id"],
                t["relation"],
                t["object_type"],
                t["object_id"],
            )
        )

    memo: dict[tuple[str, str, str, str, str], bool] = {}
    results: dict[tuple[str, str, str, str, str], bool] = {}
    for subject_tuple, permission, object_tuple in checks:
        subject = Entity(subject_tuple[0], subject_tuple[1])
        obj = Entity(object_tuple[0], object_tuple[1])
        result, _ = _compute_permission_simple(
            subject, permission, obj, direct_index, namespaces, memo
        )
        key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        results[key] = result
    return results


def _unwrap_userset(definition: Any) -> list[str]:
    """Normalize a namespace permission/relation definition into a flat
    list of userset names (Round-2 review fix for codex HIGH finding).

    Accepts the three shapes that appear in namespace configs:

    - ``None`` / ``"direct"`` / any non-iterable → ``[]`` (leaf).
    - ``["viewer", "editor"]`` (list of strings) → unchanged.
    - ``{"union": ["viewer", "editor"]}`` (dict-wrapped union) → the
      ``"union"`` member list, NOT the dict's keys. Iterating the dict
      directly yields ``"union"`` and silently denies valid access.

    Other dict shapes (``tupleToUserset``, ``intersection``, etc.) are
    not expanded in the simple fallback — they are returned as ``[]``
    so the caller falls through to direct-tuple-only matching. The
    Rust implementation handles them; this fallback is intentionally
    conservative.
    """
    if definition is None:
        return []
    if isinstance(definition, str):
        return []
    if isinstance(definition, list):
        return [m for m in definition if isinstance(m, str)]
    if isinstance(definition, dict) and isinstance(definition.get("union"), list):
        return [m for m in definition["union"] if isinstance(m, str)]
    return []


def _compute_permission_simple(
    subject: "Entity",
    permission: str,
    obj: "Entity",
    direct_index: set[tuple[str, str, str, str, str]],
    namespaces: "dict[str, ReBACNamespaceConfig]",
    memo: dict[tuple[str, str, str, str, str], bool] | None = None,
    _visited: set[tuple[str, str, str, str, str]] | None = None,
) -> tuple[bool, bool]:
    """Permission computation for the Python fallback (Issue #4240 rewrite).

    Expands both ``permissions`` (permission → usersets) and ``relations``
    (relation → union members) from the namespace config, matching the
    Zanzibar expansion model used by the Rust implementation.

    Returns ``(granted, cycle_observed_during_computation)``. The cycle
    bit propagates so callers can decide whether to memoize a False —
    round-1 review fix: a False produced under a cycle is order-dependent
    and must not be memoized, since the same relation can be True via a
    non-cyclic sibling path (cycle-break returns False locally, but a
    fresh-stack recompute of the same relation may resolve True).

    Args:
        direct_index: O(1)-lookup set of (subject_type, subject_id, relation,
            object_type, object_id) tuples derived once per bulk call.
        memo: Optional cross-recursion cache shared by the bulk wrapper —
            stores **positive answers and acyclic negatives** only. Built
            fresh per top-level wrapper call.
        _visited: in-progress (subject, perm, obj) keys to break cycles in
            cyclic namespace configs.
    """
    memo_key = (
        subject.entity_type,
        subject.entity_id,
        permission,
        obj.entity_type,
        obj.entity_id,
    )

    if memo is not None:
        cached = memo.get(memo_key)
        if cached is not None:
            # Cached entries are acyclic by construction (see write
            # rules below), so we can safely report cycle_observed=False.
            return cached, False

    # Guard against infinite recursion from cyclic configs.
    if _visited is None:
        _visited = set()
    if memo_key in _visited:
        # Cycle: return False locally and SIGNAL the cycle so the caller
        # does not memoize this False.
        return False, True
    _visited = {*_visited, memo_key}

    # 1. Direct tuple match: O(1). Always acyclic.
    if memo_key in direct_index:
        if memo is not None:
            memo[memo_key] = True
        return True, False

    namespace = namespaces.get(obj.entity_type)
    if not namespace:
        # Acyclic terminal — safe to memoize negative.
        if memo is not None:
            memo[memo_key] = False
        return False, False

    cycle_observed = False

    # 2. Expand via permissions dict: permission → list of usersets.
    # Round-2 review (codex finding HIGH): a permission may be defined as
    # ``"read": ["viewer"]`` (list) OR ``"read": {"union": ["viewer"]}``
    # (dict). Iterating the dict form directly yields the key "union"
    # instead of "viewer" — the previous code recursed on "union" as a
    # relation that doesn't exist and silently denied valid access.
    permissions_dict = namespace.config.get("permissions", {})
    perm_def = permissions_dict.get(permission)
    for member in _unwrap_userset(perm_def):
        sub_result, sub_cycle = _compute_permission_simple(
            subject, member, obj, direct_index, namespaces, memo, _visited
        )
        if sub_result:
            if memo is not None:
                memo[memo_key] = True
            return True, False
        cycle_observed = cycle_observed or sub_cycle

    # 3. Expand via relations dict: relation → list/union members.
    relations_dict = namespace.config.get("relations", {})
    relation_def = relations_dict.get(permission)
    for member in _unwrap_userset(relation_def):
        sub_result, sub_cycle = _compute_permission_simple(
            subject, member, obj, direct_index, namespaces, memo, _visited
        )
        if sub_result:
            if memo is not None:
                memo[memo_key] = True
            return True, False
        cycle_observed = cycle_observed or sub_cycle

    # Negative result: memoize only if no cycle was observed during
    # computation. A cycle-tainted False is order-dependent and may
    # become True on a fresh-stack recompute via a sibling path.
    if memo is not None and not cycle_observed:
        memo[memo_key] = False
    return False, cycle_observed


# Convenience functions for integration with existing code


def get_performance_stats() -> dict[str, Any]:
    """
    Get performance statistics (if Rust is available).

    Returns:
        Dict with performance metrics
    """
    return {
        "rust_available": RUST_AVAILABLE,
        "expected_speedup": "85x for bulk operations" if RUST_AVAILABLE else "N/A",
        "recommended_batch_size": "100-10000 checks" if RUST_AVAILABLE else "N/A",
    }


def check_permission_single_rust(
    subject_type: str,
    subject_id: str,
    permission: str,
    object_type: str,
    object_id: str,
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
) -> bool:
    """
    Check a single permission using Rust implementation with memoization.

    This function provides the same memoization benefits as bulk checks but for
    single permission checks. It's particularly useful for operations like read()
    where only one file permission needs to be checked.

    The Rust implementation has proper memoization across recursive calls, which
    prevents the exponential time complexity that causes timeouts in the Python
    implementation for deep path hierarchies.

    Args:
        subject_type: Type of subject (e.g., "user", "agent")
        subject_id: Subject identifier
        permission: Permission to check (e.g., "read", "write")
        object_type: Type of object (e.g., "file")
        object_id: Object identifier (e.g., file path)
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Dict mapping object_type -> namespace config

    Returns:
        True if permission is granted, False otherwise

    Raises:
        RuntimeError: If Rust extension is not available
    """
    if not RUST_AVAILABLE:
        raise RuntimeError(
            "Rust acceleration not available (nexus_runtime extension not present in this build; the kernel runs as a separate process via gRPC — see _rust_compat.py)"
        )

    if _compute_permission_single is None:
        raise RuntimeError(
            "Rust single permission check not available. "
            "nexus_runtime not present (kernel runs as a separate process via gRPC; see _rust_compat.py)"
        )

    try:
        import time

        start = time.perf_counter()
        result: bool = _compute_permission_single(
            subject_type,
            subject_id,
            permission,
            object_type,
            object_id,
            tuples,
            namespace_configs,
        )
        elapsed = time.perf_counter() - start
        logger.debug(
            f"[RUST-SINGLE] Permission check: {subject_type}:{subject_id} "
            f"{permission} {object_type}:{object_id} = {result} ({elapsed * 1000:.2f}ms)"
        )
        return result
    except (RuntimeError, ValueError) as e:
        logger.error(f"Rust single permission check failed: {e}", exc_info=True)
        raise


def check_permission_single_with_fallback(
    subject_type: str,
    subject_id: str,
    permission: str,
    object_type: str,
    object_id: str,
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
    force_python: bool = False,
) -> bool:
    """
    Check a single permission with automatic fallback to Python.

    This is the recommended interface for single permission checks. It uses Rust
    if available (with proper memoization), falling back to Python bulk check
    as a single-item batch if Rust is unavailable.

    Args:
        subject_type: Type of subject
        subject_id: Subject identifier
        permission: Permission to check
        object_type: Type of object
        object_id: Object identifier
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Dict mapping object_type -> namespace config
        force_python: Force use of Python implementation

    Returns:
        True if permission is granted, False otherwise
    """
    if _compute_permission_single is not None and not force_python:
        try:
            return check_permission_single_rust(
                subject_type,
                subject_id,
                permission,
                object_type,
                object_id,
                tuples,
                namespace_configs,
            )
        except (RuntimeError, ValueError) as e:
            logger.warning(f"Rust single check failed, falling back to Python: {e}")
            # Fall through to Python

    # Fallback: use Python bulk check with single item
    # This still benefits from memoization within the bulk operation
    checks = [((subject_type, subject_id), permission, (object_type, object_id))]
    results = _check_permissions_bulk_python(checks, tuples, namespace_configs)
    key = (subject_type, subject_id, permission, object_type, object_id)
    return results.get(key, False)


def estimate_speedup(num_checks: int) -> float:
    """
    Estimate speedup factor for given number of checks.

    Args:
        num_checks: Number of permission checks

    Returns:
        Expected speedup factor (e.g., 85.0 means 85x faster)
    """
    if not RUST_AVAILABLE:
        return 1.0

    # Empirical speedup curve
    if num_checks < 10:
        return 20.0  # ~20x for small batches (Python overhead)
    elif num_checks < 100:
        return 50.0  # ~50x
    else:
        return 85.0  # ~85x for large batches


def expand_subjects_rust(
    permission: str,
    object_type: str,
    object_id: str,
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
) -> list[tuple[str, str]]:
    """
    Expand subjects using Rust implementation.

    Find all subjects that have a given permission on an object.
    This is the inverse of check_permission - instead of "does X have permission on Y",
    it answers "who has permission on Y".

    Args:
        permission: Permission to expand (e.g., "read", "write")
        object_type: Type of object (e.g., "file")
        object_id: Object identifier (e.g., file path)
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Dict mapping object_type -> namespace config

    Returns:
        List of (subject_type, subject_id) tuples

    Raises:
        RuntimeError: If Rust extension is not available
    """
    if not RUST_AVAILABLE:
        raise RuntimeError(
            "Rust acceleration not available (nexus_runtime extension not present in this build; the kernel runs as a separate process via gRPC — see _rust_compat.py)"
        )

    if _expand_subjects is None:
        raise RuntimeError(
            "Rust expand_subjects not available. "
            "nexus_runtime not present (kernel runs as a separate process via gRPC; see _rust_compat.py)"
        )

    try:
        import time

        start = time.perf_counter()
        result = _expand_subjects(
            permission,
            object_type,
            object_id,
            tuples,
            namespace_configs,
        )
        elapsed = time.perf_counter() - start
        logger.debug(
            f"[RUST-EXPAND] Expand {permission} on {object_type}:{object_id} "
            f"found {len(result)} subjects ({elapsed * 1000:.2f}ms)"
        )
        # Convert from list of tuples to list of tuples (already correct format)
        return [(t[0], t[1]) for t in result]
    except (RuntimeError, ValueError) as e:
        logger.error(f"Rust expand_subjects failed: {e}", exc_info=True)
        raise


def expand_subjects_with_fallback(
    permission: str,
    object_type: str,
    object_id: str,
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
    force_python: bool = False,
) -> list[tuple[str, str]]:
    """
    Expand subjects with automatic fallback to Python.

    This is the recommended interface for subject expansion. It uses Rust
    if available, falling back to Python implementation if Rust is unavailable.

    Args:
        permission: Permission to expand
        object_type: Type of object
        object_id: Object identifier
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Dict mapping object_type -> namespace config
        force_python: Force use of Python implementation

    Returns:
        List of (subject_type, subject_id) tuples
    """
    if _expand_subjects is not None and not force_python:
        try:
            return expand_subjects_rust(
                permission,
                object_type,
                object_id,
                tuples,
                namespace_configs,
            )
        except (RuntimeError, ValueError) as e:
            logger.warning(f"Rust expand_subjects failed, falling back to Python: {e}")
            # Fall through to Python

    # Fallback: Python implementation
    # Note: The caller should implement Python fallback in rebac_manager.py
    # This is just a stub that raises NotImplementedError
    raise NotImplementedError(
        "Python fallback for expand_subjects not implemented in rebac_fast.py. "
        "Use ReBACManager._expand_permission directly."
    )


def list_objects_for_subject_rust(
    subject_type: str,
    subject_id: str,
    permission: str,
    object_type: str,
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
    path_prefix: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[tuple[str, str]]:
    """
    List objects that a subject can access using Rust implementation.

    This is the inverse of expand_subjects - instead of "who has permission on Y",
    it answers "what objects can subject X access".

    Optimized for the common case of finding files a user can read/write.

    Args:
        subject_type: Type of subject (e.g., "user", "agent")
        subject_id: Subject identifier (e.g., "alice")
        permission: Permission to check (e.g., "read", "write")
        object_type: Type of objects to find (e.g., "file")
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Dict mapping object_type -> namespace config
        path_prefix: Optional path prefix filter (e.g., "/workspace/")
        limit: Maximum number of results to return (default: 1000)
        offset: Number of results to skip for pagination (default: 0)

    Returns:
        List of (object_type, object_id) tuples that subject can access

    Raises:
        RuntimeError: If Rust extension is not available
    """
    if not RUST_AVAILABLE:
        raise RuntimeError(
            "Rust acceleration not available (nexus_runtime extension not present in this build; the kernel runs as a separate process via gRPC — see _rust_compat.py)"
        )

    if _list_objects_for_subject is None:
        raise RuntimeError(
            "Rust list_objects_for_subject not available. "
            "nexus_runtime not present (kernel runs as a separate process via gRPC; see _rust_compat.py)"
        )

    try:
        import time

        start = time.perf_counter()
        result = _list_objects_for_subject(
            subject_type,
            subject_id,
            permission,
            object_type,
            tuples,
            namespace_configs,
            path_prefix,
            limit,
            offset,
        )
        elapsed = time.perf_counter() - start
        logger.debug(
            f"[RUST-LIST-OBJECTS] List {object_type}s with {permission} for "
            f"{subject_type}:{subject_id} (prefix={path_prefix}) "
            f"found {len(result)} objects ({elapsed * 1000:.2f}ms)"
        )
        # Convert from list of tuples to list of tuples (already correct format)
        return [(t[0], t[1]) for t in result]
    except (RuntimeError, ValueError) as e:
        logger.error(f"Rust list_objects_for_subject failed: {e}", exc_info=True)
        raise


def list_objects_for_subject_with_fallback(
    subject_type: str,
    subject_id: str,
    permission: str,
    object_type: str,
    tuples: list[dict[str, Any]],
    namespace_configs: dict[str, Any],
    path_prefix: str | None = None,
    limit: int = 1000,
    offset: int = 0,
    force_python: bool = False,
) -> list[tuple[str, str]]:
    """
    List objects for subject with automatic fallback to Python.

    This is the recommended interface for listing accessible objects. It uses Rust
    if available, falling back to Python implementation if Rust is unavailable.

    Args:
        subject_type: Type of subject (e.g., "user", "agent")
        subject_id: Subject identifier
        permission: Permission to check
        object_type: Type of objects to find
        tuples: List of ReBAC relationship dictionaries
        namespace_configs: Dict mapping object_type -> namespace config
        path_prefix: Optional path prefix filter
        limit: Maximum number of results
        offset: Number of results to skip
        force_python: Force use of Python implementation

    Returns:
        List of (object_type, object_id) tuples
    """
    if _list_objects_for_subject is not None and not force_python:
        try:
            return list_objects_for_subject_rust(
                subject_type,
                subject_id,
                permission,
                object_type,
                tuples,
                namespace_configs,
                path_prefix,
                limit,
                offset,
            )
        except (RuntimeError, ValueError) as e:
            logger.warning(f"Rust list_objects_for_subject failed, falling back to Python: {e}")
            # Fall through to Python

    # Fallback: Python implementation
    # Note: The caller should implement Python fallback in rebac_manager.py
    raise NotImplementedError(
        "Python fallback for list_objects_for_subject not implemented in rebac_fast.py. "
        "Use ReBACManager.rebac_list_objects directly."
    )
