"""Bulk Permission Evaluator — In-memory graph traversal for batch checks.

Extracts the pure-function graph traversal logic from
ReBACManager._compute_permission_bulk_helper and its helpers
into standalone functions that operate on pre-fetched tuple graphs.

These functions are DB-free: they only need the tuples graph, namespace
configs, and Entity objects. This makes them testable in isolation and
reusable from both Python and Rust acceleration paths.

Related: Issue #1459 Phase 11, Performance optimization
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.rebac.domain import Entity

logger = logging.getLogger(__name__)

# Maximum traversal depth to prevent infinite recursion
MAX_DEPTH = 50


def compute_permission(
    subject: "Entity",
    permission: str,
    obj: "Entity",
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
    # Issue #4237 review round 4 (codex HIGH): a cycle-break returns
    # False, but the caller's ``_store(False)`` would memoize it. A
    # parallel non-cyclic sibling path could legitimately resolve True
    # on a separate computation, so the cached False is order-dependent
    # and wrong. Signal "cycle observed" up through nonlocal state so
    # _store() can refuse to memoize when any descendant hit a cycle.
    cycle_observed: list[bool] = [False]
    if memo_key in visited:
        logger.debug("compute_permission: Cycle detected at %s, denying", memo_key)
        cycle_observed[0] = True
        return False
    visited.add(memo_key)

    # Get namespace config
    namespace = get_namespace(obj.entity_type)
    if not namespace:
        return check_direct_relation(subject, permission, obj, tuples_graph)

    # Helper to store and return a result.
    # Round-4 fix: only memoize negatives when no cycle was observed
    # during the computation. Positives are always memoizable (the
    # recursion proved a valid grant path independent of cycle breaks).
    def _store(result: bool) -> bool:
        if bulk_memo_cache is not None and (result or not cycle_observed[0]):
            bulk_memo_cache[memo_key] = result
        return result

    # Recurse helper. Round-4: detect cycles seen in any subtree by
    # comparing the visited set before/after — if the recursive call
    # adds a key already in visited, a cycle break happened down there.
    # We achieve this more simply by propagating via the shared
    # ``cycle_observed`` list captured by closure.
    def _recurse(subj: "Entity", perm: str, target: "Entity") -> bool:
        sub_visited = visited.copy()
        result = compute_permission(
            subj,
            perm,
            target,
            zone_id,
            tuples_graph,
            get_namespace,
            depth + 1,
            sub_visited,
            bulk_memo_cache,
            memo_stats,
        )
        # If the recursion just re-entered any key already in OUR
        # visited frame, mark cycle-observed so we don't memoize the
        # parent's negative.
        if not result:
            for key in sub_visited:
                if key in visited and key != memo_key:
                    cycle_observed[0] = True
                    break
        return result

    # P0-1: Permission -> usersets (e.g., "read" -> ["viewer", "editor", "owner"]).
    # Round-4 fix (codex CRITICAL): the previous code expanded all
    # permission-level operators (union/intersection/exclusion) into a
    # flat userset list and iterated with OR semantics — turning
    # ``"read": {"intersection": ["viewer", "mfa"]}`` into an OR grant.
    # Inspect the raw shape and apply correct AND / NOT semantics, and
    # fail closed for unknown dict operators so custom namespaces can't
    # silently over-authorize.
    if namespace.has_permission(permission):
        perm_def = namespace.config.get("permissions", {}).get(permission)

        if isinstance(perm_def, list):
            logger.debug(
                "compute_permission [depth=%d]: Permission '%s' (list-OR) -> %s",
                depth,
                permission,
                perm_def,
            )
            for userset in perm_def:
                if _recurse(subject, userset, obj):
                    return _store(True)
            return _store(False)

        if isinstance(perm_def, dict):
            # Round-5 review (codex HIGH): validate operands before
            # evaluating. Empty intersection (``{"intersection": []}``)
            # previously short-circuited True because the vacuous
            # ``all([])`` is True; empty exclusion (``""``) previously
            # granted because ``not _recurse(..., "")`` evaluated False
            # for the unknown empty relation and the NOT inverted it.
            # Both are fail-open holes — refuse to evaluate.
            def _all_nonempty_strings(items: Any) -> bool:
                return (
                    isinstance(items, list)
                    and len(items) > 0
                    and all(isinstance(m, str) and m for m in items)
                )

            if "union" in perm_def:
                if not _all_nonempty_strings(perm_def["union"]):
                    logger.warning(
                        "compute_permission: empty/invalid union for '%s' in %s; failing closed",
                        permission,
                        obj.entity_type,
                    )
                    return _store(False)
                logger.debug(
                    "compute_permission [depth=%d]: Permission '%s' (union) -> %s",
                    depth,
                    permission,
                    perm_def["union"],
                )
                for userset in perm_def["union"]:
                    if _recurse(subject, userset, obj):
                        return _store(True)
                return _store(False)

            if "intersection" in perm_def:
                if not _all_nonempty_strings(perm_def["intersection"]):
                    logger.warning(
                        "compute_permission: empty/invalid intersection for '%s' in %s; failing closed",
                        permission,
                        obj.entity_type,
                    )
                    return _store(False)
                logger.debug(
                    "compute_permission [depth=%d]: Permission '%s' (intersection) -> %s",
                    depth,
                    permission,
                    perm_def["intersection"],
                )
                for userset in perm_def["intersection"]:
                    if not _recurse(subject, userset, obj):
                        return _store(False)
                return _store(True)

            if "exclusion" in perm_def:
                exclusion_target = perm_def.get("exclusion")
                if not isinstance(exclusion_target, str) or not exclusion_target:
                    logger.warning(
                        "compute_permission: empty/invalid exclusion for '%s' in %s; failing closed",
                        permission,
                        obj.entity_type,
                    )
                    return _store(False)
                logger.debug(
                    "compute_permission [depth=%d]: Permission '%s' (exclusion NOT %s)",
                    depth,
                    permission,
                    exclusion_target,
                )
                return _store(not _recurse(subject, exclusion_target, obj))

            # Unknown dict operator — fail closed, do NOT fall through
            # to get_permission_usersets()'s flattened OR (security
            # regression: this is the round-4 CRITICAL bug).
            logger.warning(
                "compute_permission: unknown permission operator for '%s' "
                "in namespace %s; failing closed. Keys: %s",
                permission,
                obj.entity_type,
                list(perm_def.keys()),
            )
            return _store(False)

        # Permission defined but in an unrecognized shape — fail closed.
        return _store(False)

    # Helper for relation-level operand validation (round-6 review).
    def _rel_nonempty(items: Any) -> bool:
        return (
            isinstance(items, list)
            and len(items) > 0
            and all(isinstance(m, str) and m for m in items)
        )

    # Union (OR of multiple relations)
    if namespace.has_union(permission):
        union_relations = namespace.get_union_relations(permission)
        if not _rel_nonempty(union_relations):
            logger.warning(
                "compute_permission: empty/invalid relation-level union for '%s' in %s; failing closed",
                permission,
                obj.entity_type,
            )
            return _store(False)
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
        # Round-6 review (codex HIGH): empty list previously short-
        # circuited True after zero iterations. Fail closed instead.
        if not _rel_nonempty(intersection_relations):
            logger.warning(
                "compute_permission: empty/invalid relation-level intersection for "
                "'%s' in %s; failing closed",
                permission,
                obj.entity_type,
            )
            return _store(False)
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
        if not isinstance(excluded_rel, str) or not excluded_rel:
            # Round-6 review: empty string previously fell through and
            # returned False from the bare ``return False`` below —
            # consistent, but make it explicit + log so it's auditable.
            logger.warning(
                "compute_permission: empty/invalid relation-level exclusion for "
                "'%s' in %s; failing closed",
                permission,
                obj.entity_type,
            )
            return _store(False)
        logger.debug(
            "compute_permission [depth=%d]: Exclusion '%s' NOT %s",
            depth,
            permission,
            excluded_rel,
        )
        return _store(not _recurse(subject, excluded_rel, obj))

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

            # Fix nexi-lab/nexus#3733 Bug A: skip Pattern 2 for ``parent``
            # tupleset. Pattern 2 finds tuples where ``obj`` is the OBJECT
            # of a tuple with this relation — which for ``parent`` returns
            # ``obj``'s CHILDREN, not its parent. That's the inverse of
            # what ``parent_owner``/``parent_viewer`` mean, and causes a
            # privilege escalation where owning any child grants access
            # to all siblings (via the parent). The same guard already
            # exists in ``zone_traversal.py``; it was missing here.
            if tupleset_relation == "parent":
                logger.debug(
                    "compute_permission [depth=%d]: skipping Pattern 2 for 'parent' tupleset "
                    "(not a group pattern, would cause privilege escalation)",
                    depth,
                )
                return _store(False)

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
    subject: "Entity",
    permission: str,
    obj: "Entity",
    tuples_graph: list[dict[str, Any]],
) -> bool:
    """Check if a direct relation tuple exists in the pre-fetched graph.

    Round-10 review (codex HIGH): also accept the wildcard subject
    ``("*", "*")`` for public grants. The single-check path
    (``ZoneAwareTraversal.has_direct_relation``) honors this — so a
    ``rebac_check`` granted but the prior bulk path denied, causing
    single-vs-bulk divergence for any public file grant.

    Returns:
        True if a matching direct tuple exists (exact subject OR
        wildcard subject).
    """
    for tuple_data in tuples_graph:
        if _has_conditions(tuple_data):
            continue
        if (
            tuple_data["relation"] != permission
            or tuple_data["object_type"] != obj.entity_type
            or tuple_data["object_id"] != obj.entity_id
            or tuple_data["subject_relation"] is not None
        ):
            continue
        # Exact match.
        if (
            tuple_data["subject_type"] == subject.entity_type
            and tuple_data["subject_id"] == subject.entity_id
        ):
            return True
        # Round-10: wildcard ``("*", "*")`` matches any subject.
        if tuple_data["subject_type"] == "*" and tuple_data["subject_id"] == "*":
            return True
    return False


def find_related_objects(
    obj: "Entity",
    tupleset_relation: str,
    tuples_graph: list[dict[str, Any]],
) -> "list[Entity]":
    """Find all objects related to obj via tupleset_relation in the graph.

    For parent inheritance: (child, "parent", parent) — obj is the child, returns parents.

    Returns:
        List of related Entity objects.
    """
    from nexus.bricks.rebac.domain import Entity as _Entity

    related = []
    for tuple_data in tuples_graph:
        if _has_conditions(tuple_data):
            continue
        if (
            tuple_data["subject_type"] == obj.entity_type
            and tuple_data["subject_id"] == obj.entity_id
            and tuple_data["relation"] == tupleset_relation
        ):
            related.append(_Entity(tuple_data["object_type"], tuple_data["object_id"]))
    return related


def find_subjects(
    obj: "Entity",
    tupleset_relation: str,
    tuples_graph: list[dict[str, Any]],
) -> "list[Entity]":
    """Find all subjects that have a relation to obj in the graph.

    For group inheritance: (group, "direct_viewer", file) — obj is file, returns groups.

    Returns:
        List of subject Entity objects.
    """
    from nexus.bricks.rebac.domain import Entity as _Entity

    subjects = []
    for tuple_data in tuples_graph:
        if _has_conditions(tuple_data):
            continue
        if (
            tuple_data["object_type"] == obj.entity_type
            and tuple_data["object_id"] == obj.entity_id
            and tuple_data["relation"] == tupleset_relation
        ):
            subjects.append(_Entity(tuple_data["subject_type"], tuple_data["subject_id"]))
    return subjects


def _has_conditions(tuple_data: dict[str, Any]) -> bool:
    """Bulk checks have no ABAC context, so conditioned tuples are unusable."""
    return bool(tuple_data.get("conditions"))
