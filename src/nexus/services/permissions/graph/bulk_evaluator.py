"""Bulk Permission Evaluator — In-memory graph traversal for batch checks.

Extracts the pure-function graph traversal logic from
EnhancedReBACManager._compute_permission_bulk_helper and its helpers
into standalone functions that operate on pre-fetched tuple graphs.

These functions are DB-free: they only need the tuples graph, namespace
configs, and Entity objects. This makes them testable in isolation and
reusable from both Python and Rust acceleration paths.

Related: Issue #1459 Phase 11, Performance optimization
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.rebac import Entity

logger = logging.getLogger(__name__)

# Maximum traversal depth to prevent infinite recursion
MAX_DEPTH = 50


def compute_permission(
    subject: Entity,
    permission: str,
    obj: Entity,
    zone_id: str,
    tuples_graph: list[dict[str, Any]],
    get_namespace: Any,
    depth: int = 0,
    visited: set[tuple[str, str, str, str, str]] | None = None,
    bulk_memo_cache: dict[tuple[str, str, str, str, str], bool] | None = None,
    memo_stats: dict[str, int] | None = None,
) -> bool:
    """Compute permission using pre-fetched tuples graph with full in-memory traversal.

    Handles: direct relations, union, intersection, exclusion,
    tupleToUserset (parent/group inheritance).

    Args:
        subject: Subject entity
        permission: Permission to check
        obj: Object entity
        zone_id: Zone ID
        tuples_graph: Pre-fetched list of all relevant tuples
        get_namespace: Callable that returns NamespaceConfig for a given entity type
        depth: Current traversal depth (for cycle detection)
        visited: Set of visited nodes (for cycle detection)
        bulk_memo_cache: Shared memoization cache for bulk operations
        memo_stats: Stats tracker for cache hits/misses

    Returns:
        True if permission is granted
    """
    # Initialize visited set on first call
    if visited is None:
        visited = set()

    # OPTIMIZATION: Check memoization cache first
    memo_key = (
        subject.entity_type,
        subject.entity_id,
        permission,
        obj.entity_type,
        obj.entity_id,
    )
    if bulk_memo_cache is not None and memo_key in bulk_memo_cache:
        if memo_stats is not None:
            memo_stats["hits"] += 1
            if memo_stats["hits"] % 100 == 0:
                logger.debug(
                    "[MEMO HIT #%d] %s:%s %s on %s:%s",
                    memo_stats["hits"],
                    subject.entity_type,
                    subject.entity_id,
                    permission,
                    obj.entity_type,
                    obj.entity_id,
                )
        return bulk_memo_cache[memo_key]

    # Cache miss - will need to compute
    if memo_stats is not None:
        memo_stats["misses"] += 1
        if depth > memo_stats.get("max_depth", 0):
            memo_stats["max_depth"] = depth

    # Depth limit check
    if depth > MAX_DEPTH:
        logger.warning(
            "compute_permission: Depth limit exceeded (%d > %d), denying",
            depth,
            MAX_DEPTH,
        )
        return False

    # Cycle detection
    if memo_key in visited:
        logger.debug("compute_permission: Cycle detected at %s, denying", memo_key)
        return False
    visited.add(memo_key)

    # Get namespace config
    namespace = get_namespace(obj.entity_type)
    if not namespace:
        return check_direct_relation(subject, permission, obj, tuples_graph)

    # Helper to store and return a result
    def _store(result: bool) -> bool:
        if bulk_memo_cache is not None:
            bulk_memo_cache[memo_key] = result
        return result

    # Recurse helper
    def _recurse(subj: Entity, perm: str, target: Entity) -> bool:
        return compute_permission(
            subj,
            perm,
            target,
            zone_id,
            tuples_graph,
            get_namespace,
            depth + 1,
            visited.copy(),
            bulk_memo_cache,
            memo_stats,
        )

    # P0-1: Permission -> usersets (e.g., "read" -> ["viewer", "editor", "owner"])
    if namespace.has_permission(permission):
        usersets = namespace.get_permission_usersets(permission)
        logger.debug(
            "compute_permission [depth=%d]: Permission '%s' expands to usersets: %s",
            depth,
            permission,
            usersets,
        )
        for userset in usersets:
            if _recurse(subject, userset, obj):
                return _store(True)
        return _store(False)

    # Union (OR of multiple relations)
    if namespace.has_union(permission):
        union_relations = namespace.get_union_relations(permission)
        logger.debug(
            "compute_permission [depth=%d]: Union '%s' -> %s",
            depth,
            permission,
            union_relations,
        )
        for rel in union_relations:
            if _recurse(subject, rel, obj):
                return _store(True)
        return _store(False)

    # Intersection (AND of multiple relations)
    if namespace.has_intersection(permission):
        intersection_relations = namespace.get_intersection_relations(permission)
        logger.debug(
            "compute_permission [depth=%d]: Intersection '%s' -> %s",
            depth,
            permission,
            intersection_relations,
        )
        for rel in intersection_relations:
            if not _recurse(subject, rel, obj):
                return _store(False)
        return _store(True)

    # Exclusion (NOT relation)
    if namespace.has_exclusion(permission):
        excluded_rel = namespace.get_exclusion_relation(permission)
        if excluded_rel:
            logger.debug(
                "compute_permission [depth=%d]: Exclusion '%s' NOT %s",
                depth,
                permission,
                excluded_rel,
            )
            return _store(not _recurse(subject, excluded_rel, obj))
        return False

    # tupleToUserset (indirect relation via another object)
    if namespace.has_tuple_to_userset(permission):
        ttu = namespace.get_tuple_to_userset(permission)
        logger.debug(
            "compute_permission [depth=%d]: tupleToUserset '%s' -> %s",
            depth,
            permission,
            ttu,
        )
        if ttu:
            tupleset_relation = ttu["tupleset"]
            computed_userset = ttu["computedUserset"]

            # Pattern 1 (parent-style): (obj, tupleset_relation, ?)
            related_objects = find_related_objects(obj, tupleset_relation, tuples_graph)
            logger.debug(
                "compute_permission [depth=%d]: Pattern 1 (parent) found %d related objects via '%s'",
                depth,
                len(related_objects),
                tupleset_relation,
            )

            for related_obj in related_objects:
                if _recurse(subject, computed_userset, related_obj):
                    logger.debug(
                        "compute_permission [depth=%d]: GRANTED via tupleToUserset parent through %s",
                        depth,
                        related_obj,
                    )
                    return _store(True)

            # Pattern 2 (group-style): (?, tupleset_relation, obj)
            related_subjects = find_subjects(obj, tupleset_relation, tuples_graph)
            logger.debug(
                "compute_permission [depth=%d]: Pattern 2 (group) found %d subjects with '%s' on obj",
                depth,
                len(related_subjects),
                tupleset_relation,
            )

            for related_subj in related_subjects:
                if _recurse(subject, computed_userset, related_subj):
                    logger.debug(
                        "compute_permission [depth=%d]: GRANTED via tupleToUserset group through %s",
                        depth,
                        related_subj,
                    )
                    return _store(True)

            logger.debug(
                "compute_permission [depth=%d]: No related objects/subjects granted permission",
                depth,
            )
            return _store(False)
        return False

    # Direct relation check (base case)
    return _store(check_direct_relation(subject, permission, obj, tuples_graph))


def check_direct_relation(
    subject: Entity,
    permission: str,
    obj: Entity,
    tuples_graph: list[dict[str, Any]],
) -> bool:
    """Check if a direct relation tuple exists in the pre-fetched graph.

    Returns:
        True if direct tuple exists.
    """
    for tuple_data in tuples_graph:
        if (
            tuple_data["subject_type"] == subject.entity_type
            and tuple_data["subject_id"] == subject.entity_id
            and tuple_data["relation"] == permission
            and tuple_data["object_type"] == obj.entity_type
            and tuple_data["object_id"] == obj.entity_id
            and tuple_data["subject_relation"] is None  # Direct relation only
        ):
            return True
    return False


def find_related_objects(
    obj: Entity,
    tupleset_relation: str,
    tuples_graph: list[dict[str, Any]],
) -> list[Entity]:
    """Find all objects related to obj via tupleset_relation in the graph.

    For parent inheritance: (child, "parent", parent) — obj is the child, returns parents.

    Returns:
        List of related Entity objects.
    """
    from nexus.core.rebac import Entity as _Entity

    related = []
    for tuple_data in tuples_graph:
        if (
            tuple_data["subject_type"] == obj.entity_type
            and tuple_data["subject_id"] == obj.entity_id
            and tuple_data["relation"] == tupleset_relation
        ):
            related.append(_Entity(tuple_data["object_type"], tuple_data["object_id"]))
    return related


def find_subjects(
    obj: Entity,
    tupleset_relation: str,
    tuples_graph: list[dict[str, Any]],
) -> list[Entity]:
    """Find all subjects that have a relation to obj in the graph.

    For group inheritance: (group, "direct_viewer", file) — obj is file, returns groups.

    Returns:
        List of subject Entity objects.
    """
    from nexus.core.rebac import Entity as _Entity

    subjects = []
    for tuple_data in tuples_graph:
        if (
            tuple_data["object_type"] == obj.entity_type
            and tuple_data["object_id"] == obj.entity_id
            and tuple_data["relation"] == tupleset_relation
        ):
            subjects.append(_Entity(tuple_data["subject_type"], tuple_data["subject_id"]))
    return subjects
