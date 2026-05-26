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

    Issue #4240: the previous implementation scanned the full ``tuples``
    list per check (O(N × T)). For the reported 5-result search with a
    few hundred tuples per zone that was ~4500ms total filter latency.

    This rewrite is O(N + T) per bulk call:

    * Build a set-based direct-grant index ONCE (T work, hashable keys).
      Userset subjects (``subject_relation`` set) and conditioned tuples
      are excluded — they cannot grant directly, matching the prior
      behavior at the linear-scan filter (see ``_compute_permission_simple``
      conditions in the old code).
    * Memoize positive AND negative answers across recursive expansion
      in the same bulk call (the prior ``_visited`` set only prevented
      re-entry; siblings re-explored the same subtree from scratch).
    """
    from nexus.bricks.rebac.domain import Entity, NamespaceConfig

    # Convert namespace configs to proper format
    namespaces: dict[str, ReBACNamespaceConfig] = {}
    for obj_type, config_dict in namespace_configs.items():
        if isinstance(config_dict, NamespaceConfig):
            namespaces[obj_type] = config_dict
        else:
            # Convert dict to NamespaceConfig - config_dict should contain 'relations' and 'permissions'
            namespaces[obj_type] = NamespaceConfig(
                namespace_id="",  # Will be auto-generated
                object_type=obj_type,
                config=config_dict,  # Pass the whole dict as config
            )

    # Issue #4240: pre-index direct grants for O(1) per-check lookup.
    direct_index: set[tuple[str, str, str, str, str]] = set()
    for t in tuples:
        if t.get("subject_relation") is not None:
            continue  # usersets cannot grant directly in the simple fallback
        if t.get("conditions"):
            continue  # conditioned tuples are fail-closed without ABAC context
        direct_index.add(
            (
                t["subject_type"],
                t["subject_id"],
                t["relation"],
                t["object_type"],
                t["object_id"],
            )
        )

    # Memo across the whole bulk call: completed (subject, perm, obj) → bool.
    memo: dict[tuple[str, str, str, str, str], bool] = {}

    # Compute each check.
    results: dict[tuple[str, str, str, str, str], bool] = {}
    for subject_tuple, permission, object_tuple in checks:
        subject = Entity(subject_tuple[0], subject_tuple[1])
        obj = Entity(object_tuple[0], object_tuple[1])

        result = _compute_permission_simple(
            subject, permission, obj, direct_index, namespaces, memo
        )

        key = (subject.entity_type, subject.entity_id, permission, obj.entity_type, obj.entity_id)
        results[key] = result

    return results


def _compute_permission_simple(
    subject: "Entity",
    permission: str,
    obj: "Entity",
    direct_index: set[tuple[str, str, str, str, str]],
    namespaces: "dict[str, ReBACNamespaceConfig]",
    memo: dict[tuple[str, str, str, str, str], bool] | None = None,
    _visited: set[tuple[str, str, str, str, str]] | None = None,
) -> bool:
    """Permission computation for the Python fallback (Issue #4240 rewrite).

    Expands both ``permissions`` (permission → usersets) and ``relations``
    (relation → union members) from the namespace config, matching the
    Zanzibar expansion model used by the Rust implementation.

    Args:
        direct_index: O(1)-lookup set of (subject_type, subject_id, relation,
            object_type, object_id) tuples derived once per bulk call.
        memo: Optional cross-recursion cache shared by the bulk wrapper —
            stores completed positive AND negative answers so sibling
            expansions don't re-explore the same subtree. Built fresh per
            top-level wrapper call.
        _visited: in-progress (subject, perm, obj) keys to break cycles in
            cyclic namespace configs without poisoning ``memo`` with a
            premature False.
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
            return cached

    # Guard against infinite recursion from cyclic configs.
    if _visited is None:
        _visited = set()
    if memo_key in _visited:
        # Cycle: return False locally but do NOT memoize — a parallel
        # non-cyclic path may still produce True.
        return False
    _visited = {*_visited, memo_key}

    # 1. Direct tuple match: O(1).
    if memo_key in direct_index:
        if memo is not None:
            memo[memo_key] = True
        return True

    namespace = namespaces.get(obj.entity_type)
    if not namespace:
        if memo is not None:
            memo[memo_key] = False
        return False

    # 2. Expand via permissions dict: permission → list of usersets.
    permissions_dict = namespace.config.get("permissions", {})
    if permission in permissions_dict:
        for userset in permissions_dict[permission]:
            if _compute_permission_simple(
                subject, userset, obj, direct_index, namespaces, memo, _visited
            ):
                if memo is not None:
                    memo[memo_key] = True
                return True

    # 3. Expand via relations dict: relation → union members.
    relations_dict = namespace.config.get("relations", {})
    relation_def = relations_dict.get(permission)
    if isinstance(relation_def, dict) and "union" in relation_def:
        for member in relation_def["union"]:
            if _compute_permission_simple(
                subject, member, obj, direct_index, namespaces, memo, _visited
            ):
                if memo is not None:
                    memo[memo_key] = True
                return True

    if memo is not None:
        memo[memo_key] = False
    return False


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
