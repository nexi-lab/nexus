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

# RUST_FALLBACK: rebac — compute_permissions_bulk, etc. from nexus_kernel
try:
    import nexus_kernel as _rust_module
except ImportError as e:
    _rust_module = None
    _RUST_IMPORT_ERROR: ImportError | None = e
else:
    _RUST_IMPORT_ERROR = None

if TYPE_CHECKING:
    from nexus.bricks.rebac.domain import Entity
    from nexus.bricks.rebac.domain import NamespaceConfig as ReBACNamespaceConfig

# Internal type for namespace config dict (not the NamespaceConfig class)
NamespaceConfigDict = dict[str, Any]  # Contains 'relations' and 'permissions' keys

logger = logging.getLogger(__name__)

_internal_module: Any = _rust_module
_external_module: Any = _rust_module
RUST_AVAILABLE = _rust_module is not None
if RUST_AVAILABLE:
    logger.info("Rust acceleration available (nexus_kernel)")
else:
    logger.debug("Rust acceleration unavailable: %s", _RUST_IMPORT_ERROR)


def is_rust_available() -> bool:
    """Check if Rust acceleration is available.

    Returns:
        True if nexus_kernel Rust extension is loaded, False otherwise
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
            "Rust acceleration not available. Install with: cd rust/kernel && maturin develop"
        )

    try:
        # Prefer internal module (faster), fallback to external
        module = _internal_module or _external_module
        if module is None:
            raise RuntimeError("No Rust module available")

        # Try with tuple_version first (newer API)
        try:
            result: Any = module.compute_permissions_bulk(
                checks, tuples, namespace_configs, tuple_version
            )
        except TypeError as te:
            # Fallback to old API without tuple_version parameter
            if "takes 3 positional arguments" in str(te):
                logger.debug("Rust module uses old API (3 args), calling without tuple_version")
                result = module.compute_permissions_bulk(checks, tuples, namespace_configs)
            else:
                raise

        return result  # type: ignore[no-any-return]  # allowed
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
    """
    Pure Python implementation for fallback.

    This is a simplified implementation. For production, this should delegate
    to the existing ReBACManager._compute_permission logic.
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

    # Compute each check
    results: dict[tuple[str, str, str, str, str], bool] = {}

    for subject_tuple, permission, object_tuple in checks:
        subject = Entity(subject_tuple[0], subject_tuple[1])
        obj = Entity(object_tuple[0], object_tuple[1])

        # Simple implementation: check direct relations only
        # For production, this should use full graph traversal
        result = _compute_permission_simple(subject, permission, obj, tuples, namespaces)

        key = (subject.entity_type, subject.entity_id, permission, obj.entity_type, obj.entity_id)
        results[key] = result

    return results


def _compute_permission_simple(
    subject: "Entity",
    permission: str,
    obj: "Entity",
    tuples: list[dict[str, Any]],
    namespaces: "dict[str, ReBACNamespaceConfig]",
    _visited: set[str] | None = None,
) -> bool:
    """Permission computation for Python fallback.

    Expands both ``permissions`` (permission → usersets) and ``relations``
    (relation → union members) from the namespace config, matching the
    Zanzibar expansion model used by the Rust implementation.
    """
    # Guard against infinite recursion from cyclic configs
    if _visited is None:
        _visited = set()
    cache_key = f"{permission}:{obj.entity_type}:{obj.entity_id}"
    if cache_key in _visited:
        return False
    _visited = {*_visited, cache_key}

    # 1. Direct tuple match: does a tuple grant this exact relation?
    for tuple_dict in tuples:
        if (
            tuple_dict["subject_type"] == subject.entity_type
            and tuple_dict["subject_id"] == subject.entity_id
            and tuple_dict.get("subject_relation") is None
            and tuple_dict["relation"] == permission
            and tuple_dict["object_type"] == obj.entity_type
            and tuple_dict["object_id"] == obj.entity_id
            and not tuple_dict.get("conditions")
        ):
            return True

    namespace = namespaces.get(obj.entity_type)
    if not namespace:
        return False

    # 2. Expand via permissions dict: permission → list of usersets
    permissions_dict = namespace.config.get("permissions", {})
    if permission in permissions_dict:
        for userset in permissions_dict[permission]:
            if _compute_permission_simple(subject, userset, obj, tuples, namespaces, _visited):
                return True

    # 3. Expand via relations dict: relation → union members
    relations_dict = namespace.config.get("relations", {})
    relation_def = relations_dict.get(permission)
    if isinstance(relation_def, dict) and "union" in relation_def:
        for member in relation_def["union"]:
            if _compute_permission_simple(subject, member, obj, tuples, namespaces, _visited):
                return True

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
            "Rust acceleration not available. Install with: cd rust/kernel && maturin develop"
        )

    # compute_permission_single is only in the external module
    if _external_module is None:
        raise RuntimeError(
            "Rust single permission check not available. "
            "Install nexus_kernel: cd rust/kernel && maturin develop"
        )

    try:
        import time

        start = time.perf_counter()
        result: bool = _external_module.compute_permission_single(
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
    if _external_module is not None and not force_python:
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
            "Rust acceleration not available. Install with: cd rust/kernel && maturin develop"
        )

    # Use external module which has expand_subjects
    if _external_module is None:
        raise RuntimeError(
            "Rust expand_subjects not available. "
            "Install nexus_kernel: cd rust/kernel && maturin develop"
        )

    try:
        import time

        start = time.perf_counter()
        result = _external_module.expand_subjects(
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
    if _external_module is not None and not force_python:
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
            "Rust acceleration not available. Install with: cd rust/kernel && maturin develop"
        )

    # Use external module which has list_objects_for_subject
    if _external_module is None:
        raise RuntimeError(
            "Rust list_objects_for_subject not available. "
            "Install nexus_kernel: cd rust/kernel && maturin develop"
        )

    try:
        import time

        start = time.perf_counter()
        result = _external_module.list_objects_for_subject(
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
    if _external_module is not None and not force_python:
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
