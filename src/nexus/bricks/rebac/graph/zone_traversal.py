"""Zone-aware permission graph traversal with P0-5 limits.

Extracts the zone-scoped graph traversal and direct relation checking
from ReBACManager into focused, testable functions.

Functions:
- compute_permission_zone_aware: Recursive graph traversal with limits
- has_direct_relation_zone_aware: DB-backed tuple lookup with ABAC/wildcards
- find_related_objects_zone_aware: Find related objects (parent pattern)
- find_subjects_with_relation_zone_aware: Find subjects (group pattern)

Related: Issue #1459 Phase 15+, performance optimization
"""

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from nexus.bricks.rebac.domain import WILDCARD_SUBJECT, Entity
from nexus.contracts.rebac_types import (
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
)
from nexus.storage.models.permissions import ReBACTupleModel as RT

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

    from nexus.bricks.rebac.consistency.zone_manager import ZoneManager
    from nexus.bricks.rebac.domain import NamespaceConfig

logger = logging.getLogger(__name__)


class ZoneAwareTraversal:
    """Zone-aware permission graph traversal with P0-5 limits.

    Encapsulates the recursive graph traversal algorithms that operate on
    zone-scoped tuples in the database. Handles permission mapping, union
    expansion, tupleToUserset (parent/group patterns), cycle detection,
    memoization, and ABAC condition evaluation.

    Args:
        engine: SQLAlchemy engine for database connections
        get_namespace: Callable (entity_type) -> NamespaceConfig | None
        evaluate_conditions: Callable (conditions, context) -> bool
        zone_manager: ZoneManager for cross-zone policy decisions
        enable_graph_limits: Whether to enforce P0-5 graph limits
    """

    def __init__(
        self,
        engine: "Engine",
        get_namespace: "Callable[[str], NamespaceConfig | None]",
        evaluate_conditions: "Callable[[dict[str, Any] | None, dict[str, Any] | None], bool]",
        zone_manager: "ZoneManager",
        enable_graph_limits: bool = True,
    ) -> None:
        self._engine = engine
        self._get_namespace = get_namespace
        self._evaluate_conditions = evaluate_conditions
        self._zone_manager = zone_manager
        self.enable_graph_limits = enable_graph_limits

    # ------------------------------------------------------------------
    # Core graph traversal
    # ------------------------------------------------------------------

    def compute_permission(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        zone_id: str,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        start_time: float,
        stats: TraversalStats,
        context: dict[str, Any] | None = None,
        memo: dict[tuple[str, str, str, str, str], bool] | None = None,
    ) -> bool:
        """Compute permission with P0-5 limits enforced at each step.

        PERF FIX: Added memo dict for cross-branch memoization.
        - visited: prevents cycles within a single path (copied per branch)
        - memo: caches results across ALL branches (shared, never copied)
        """
        indent = "  " * depth

        # Initialize memo on first call
        if memo is None:
            memo = {}

        # PERF FIX: Check memo cache first (shared across all branches)
        memo_key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        if memo_key in memo:
            cached_result = memo[memo_key]
            stats.cache_hits += 1
            logger.debug(f"{indent}[MEMO-HIT] {memo_key} = {cached_result}")
            return cached_result

        logger.debug(
            f"{indent}┌─[PERM-CHECK depth={depth}] {subject.entity_type}:{subject.entity_id} → '{permission}' → {obj.entity_type}:{obj.entity_id}"
        )

        # P0-5: Check execution time (using perf_counter for monotonic measurement)
        if self.enable_graph_limits:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            if elapsed_ms > GraphLimits.MAX_EXECUTION_TIME_MS:
                raise GraphLimitExceeded("timeout", GraphLimits.MAX_EXECUTION_TIME_MS, elapsed_ms)

        # P0-5: Check depth limit
        if depth > GraphLimits.MAX_DEPTH:
            raise GraphLimitExceeded("depth", GraphLimits.MAX_DEPTH, depth)

        stats.max_depth_reached = max(stats.max_depth_reached, depth)

        # Check for cycles (within this traversal path only)
        visit_key = memo_key  # Same key format
        if visit_key in visited:
            logger.debug(f"{indent}← CYCLE DETECTED, returning False")
            return False
        visited.add(visit_key)
        stats.nodes_visited += 1

        # P0-5: Check visited nodes limit
        if self.enable_graph_limits and len(visited) > GraphLimits.MAX_VISITED_NODES:
            raise GraphLimitExceeded("nodes", GraphLimits.MAX_VISITED_NODES, len(visited))

        # Get namespace config
        namespace = self._get_namespace(obj.entity_type)
        if not namespace:
            logger.debug(f"{indent}  No namespace for {obj.entity_type}, checking direct relation")
            stats.queries += 1
            if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
            result = self.has_direct_relation(subject, permission, obj, zone_id, context)
            logger.debug(f"{indent}← RESULT: {result}")
            memo[memo_key] = result
            return result

        # Helper to store and return a memoized result
        def _store(result: bool) -> bool:
            memo[memo_key] = result
            return result

        # Recurse helper
        def _recurse(subj: Entity, perm: str, target: Entity) -> bool:
            return self.compute_permission(
                subj,
                perm,
                target,
                zone_id,
                visited.copy(),
                depth + 1,
                start_time,
                stats,
                context,
                memo,
            )

        # FIX: Check if permission is a mapped permission (e.g., "write" -> ["editor", "owner"])
        if namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            if usersets:
                logger.debug(
                    f"{indent}├─[PERM-MAPPING] Permission '{permission}' maps to relations: {usersets}"
                )
                for i, relation in enumerate(usersets):
                    logger.debug(
                        f"{indent}├─[PERM-REL {i + 1}/{len(usersets)}] Checking relation '{relation}' for permission '{permission}'"
                    )
                    try:
                        result = _recurse(subject, relation, obj)
                        logger.debug(f"{indent}│ └─[RESULT] '{relation}' = {result}")
                        if result:
                            logger.debug(f"{indent}└─[✅ GRANTED] via relation '{relation}'")
                            return _store(True)
                    except (RuntimeError, ValueError) as e:
                        logger.error(
                            f"{indent}│ └─[ERROR] Exception while checking '{relation}': {type(e).__name__}: {e}"
                        )
                        raise
                logger.debug(
                    f"{indent}└─[❌ DENIED] No relations granted access for permission '{permission}'"
                )
                return _store(False)

        # If permission is not mapped, try as a direct relation
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            logger.debug(
                f"{indent}  No relation config for '{permission}', checking direct relation"
            )
            stats.queries += 1
            if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
            result = self.has_direct_relation(subject, permission, obj, zone_id, context)
            logger.debug(f"{indent}← RESULT: {result}")
            memo[memo_key] = result
            return result

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            logger.debug(f"{indent}├─[UNION] Relation '{permission}' expands to: {union_relations}")

            # P0-5: Check fan-out limit
            if self.enable_graph_limits and len(union_relations) > GraphLimits.MAX_FAN_OUT:
                raise GraphLimitExceeded("fan_out", GraphLimits.MAX_FAN_OUT, len(union_relations))

            for i, rel in enumerate(union_relations):
                logger.debug(
                    f"{indent}│ ├─[UNION {i + 1}/{len(union_relations)}] Checking: '{rel}'"
                )
                try:
                    result = _recurse(subject, rel, obj)
                    logger.debug(f"{indent}│ │ └─[RESULT] '{rel}' = {result}")
                    if result:
                        logger.debug(f"{indent}└─[✅ GRANTED] via union member '{rel}'")
                        return _store(True)
                except GraphLimitExceeded as e:
                    logger.error(
                        f"{indent}[depth={depth}]   [{i + 1}/{len(union_relations)}] GraphLimitExceeded while checking '{rel}': limit_type={e.limit_type}, limit_value={e.limit_value}, actual_value={e.actual_value}"
                    )
                    raise
                except (RuntimeError, ValueError) as e:
                    logger.error(
                        f"{indent}[depth={depth}]   [{i + 1}/{len(union_relations)}] Unexpected exception while checking '{rel}': {type(e).__name__}: {e}"
                    )
                    raise
            logger.debug(f"{indent}└─[❌ DENIED] - no union members granted access")
            return _store(False)

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]
                logger.debug(
                    f"{indent}├─[TTU] '{permission}' = tupleToUserset(tupleset='{tupleset_relation}', computed='{computed_userset}')"
                )

                # Pattern 1 (parent-style): Find objects where (obj, tupleset_relation, ?)
                stats.queries += 1
                if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                    raise GraphLimitExceeded(
                        "queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries
                    )

                related_objects = self.find_related_objects(
                    obj, tupleset_relation, zone_id, context
                )
                logger.debug(
                    f"{indent}│ ├─[TTU-PARENT] Found {len(related_objects)} objects via '{tupleset_relation}': {[f'{o.entity_type}:{o.entity_id}' for o in related_objects]}"
                )

                # P0-5: Check fan-out limit
                if self.enable_graph_limits and len(related_objects) > GraphLimits.MAX_FAN_OUT:
                    raise GraphLimitExceeded(
                        "fan_out", GraphLimits.MAX_FAN_OUT, len(related_objects)
                    )

                for related_obj in related_objects:
                    logger.debug(
                        f"{indent}  Checking '{computed_userset}' on related object {related_obj.entity_type}:{related_obj.entity_id}"
                    )
                    if _recurse(subject, computed_userset, related_obj):
                        logger.debug(
                            f"{indent}← RESULT: True (via tupleToUserset parent pattern on {related_obj.entity_type}:{related_obj.entity_id})"
                        )
                        return _store(True)

                # Pattern 2 (group-style): Find subjects where (?, tupleset_relation, obj)
                # IMPORTANT: Only apply Pattern 2 for group membership patterns
                # NOT for parent relations which would cause exponential blow-up
                if tupleset_relation == "parent":
                    logger.debug(
                        f"{indent}│ └─[TTU-SKIP] Skipping Pattern 2 for 'parent' tupleset (not a group pattern)"
                    )
                    return _store(False)

                stats.queries += 1
                if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                    raise GraphLimitExceeded(
                        "queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries
                    )

                related_subjects = self.find_subjects(obj, tupleset_relation, zone_id, context)
                logger.debug(
                    f"{indent}[depth={depth}]   Pattern 2 (group): Found {len(related_subjects)} subjects with '{tupleset_relation}' on obj: {[f'{s.entity_type}:{s.entity_id}' for s in related_subjects]}"
                )

                # P0-5: Check fan-out limit for group pattern
                if self.enable_graph_limits and len(related_subjects) > GraphLimits.MAX_FAN_OUT:
                    raise GraphLimitExceeded(
                        "fan_out", GraphLimits.MAX_FAN_OUT, len(related_subjects)
                    )

                for related_subj in related_subjects:
                    logger.debug(
                        f"{indent}  Checking if {subject} has '{computed_userset}' on {related_subj.entity_type}:{related_subj.entity_id}"
                    )
                    if _recurse(subject, computed_userset, related_subj):
                        logger.debug(
                            f"{indent}← RESULT: True (via tupleToUserset group pattern on {related_subj.entity_type}:{related_subj.entity_id})"
                        )
                        return _store(True)

            logger.debug(f"{indent}← RESULT: False (tupleToUserset found no access)")
            return _store(False)

        # Direct relation check (fallback)
        logger.debug(f"{indent}[depth={depth}] Checking direct relation (fallback)")
        stats.queries += 1
        if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
            raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
        result = self.has_direct_relation(subject, permission, obj, zone_id, context)
        logger.debug(f"{indent}← [EXIT depth={depth}] Direct relation result: {result}")
        memo[memo_key] = result
        return result

    # ------------------------------------------------------------------
    # Direct relation check
    # ------------------------------------------------------------------

    def has_direct_relation(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check if subject has direct relation to object (zone-scoped).

        Handles: concrete tuples, ABAC conditions, cross-zone shares,
        wildcard/public access, userset-as-subject recursive checks.

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            zone_id: Zone ID to scope the query
            context: Optional ABAC context for condition evaluation

        Returns:
            True if direct relation exists within the zone
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[DIRECT-CHECK] Checking: (%s:%s) has '%s' on (%s:%s)? zone=%s",
                subject.entity_type,
                subject.entity_id,
                relation,
                obj.entity_type,
                obj.entity_id,
                zone_id,
            )

        now = datetime.now(UTC)
        expires_filter = or_(RT.expires_at.is_(None), RT.expires_at >= now)

        with self._engine.connect() as conn:
            # Check for direct concrete subject tuple (with ABAC conditions support)
            stmt = select(RT.tuple_id, RT.conditions).where(
                RT.subject_type == subject.entity_type,
                RT.subject_id == subject.entity_id,
                RT.relation == relation,
                RT.object_type == obj.entity_type,
                RT.object_id == obj.entity_id,
                RT.zone_id == zone_id,
                RT.subject_relation.is_(None),
                expires_filter,
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[DIRECT-CHECK] Query: subject=(%s,%s) rel=%s obj=(%s,%s) zone=%s",
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    zone_id,
                )

            row = conn.execute(stmt).first()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[DIRECT-CHECK] Query result row: %s", row)
            if row and self._conditions_allow(row.conditions, context):
                return True

            # Cross-zone check for shared-* relations (PR #647, #648)
            if self._zone_manager.is_cross_zone_readable(relation):
                cross_zone_stmt = select(RT.tuple_id, RT.conditions).where(
                    RT.subject_type == subject.entity_type,
                    RT.subject_id == subject.entity_id,
                    RT.relation == relation,
                    RT.object_type == obj.entity_type,
                    RT.object_id == obj.entity_id,
                    RT.subject_relation.is_(None),
                    expires_filter,
                )
                cross_zone_allowed = False
                for cross_zone_row in conn.execute(cross_zone_stmt):
                    if self._conditions_allow(cross_zone_row.conditions, context):
                        cross_zone_allowed = True
                        break
                if cross_zone_allowed:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Cross-zone share found: %s -> %s -> %s",
                            subject,
                            relation,
                            obj,
                        )
                    return True

            # Check for wildcard/public access (*:*) - Issue #1064
            if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
                wildcard_stmt = select(RT.tuple_id, RT.conditions).where(
                    RT.subject_type == WILDCARD_SUBJECT[0],
                    RT.subject_id == WILDCARD_SUBJECT[1],
                    RT.relation == relation,
                    RT.object_type == obj.entity_type,
                    RT.object_id == obj.entity_id,
                    RT.subject_relation.is_(None),
                    expires_filter,
                )
                wildcard_allowed = False
                for wildcard_row in conn.execute(wildcard_stmt):
                    if self._conditions_allow(wildcard_row.conditions, context):
                        wildcard_allowed = True
                        break
                if wildcard_allowed:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "[DIRECT-CHECK] Wildcard access found: *:* -> %s -> %s",
                            relation,
                            obj,
                        )
                    return True

            # Check for userset-as-subject tuple (e.g., group#member)
            userset_stmt = select(
                RT.subject_type,
                RT.subject_id,
                RT.subject_relation,
                RT.conditions,
            ).where(
                RT.relation == relation,
                RT.object_type == obj.entity_type,
                RT.object_id == obj.entity_id,
                RT.subject_relation.isnot(None),
                RT.zone_id == zone_id,
                expires_filter,
            )

            # BUGFIX (Issue #1): Use recursive ReBAC evaluation instead of direct SQL
            for userset_row in conn.execute(userset_stmt):
                if not self._conditions_allow(userset_row.conditions, context):
                    continue

                userset_type = userset_row.subject_type
                userset_id = userset_row.subject_id
                userset_relation = userset_row.subject_relation

                sub_stats = TraversalStats()
                userset_entity = Entity(userset_type, userset_id)

                try:
                    if self.compute_permission(
                        subject=subject,
                        permission=userset_relation,
                        obj=userset_entity,
                        zone_id=zone_id,
                        visited=set(),
                        depth=0,
                        start_time=time.perf_counter(),
                        stats=sub_stats,
                        context=context,
                    ):
                        return True
                except GraphLimitExceeded:
                    logger.warning(
                        f"Userset check hit limits: {subject} -> {userset_relation} -> {userset_entity}, skipping"
                    )
                    continue

            return False

    def _conditions_allow(self, conditions_json: Any, context: dict[str, Any] | None) -> bool:
        """Return True when tuple conditions are empty or satisfied."""
        if not conditions_json:
            return True

        try:
            conditions = (
                json.loads(conditions_json) if isinstance(conditions_json, str) else conditions_json
            )
        except (json.JSONDecodeError, TypeError):
            return True

        return self._evaluate_conditions(conditions, context)

    # ------------------------------------------------------------------
    # Zone-scoped tuple queries
    # ------------------------------------------------------------------

    def find_related_objects(
        self,
        obj: Entity,
        relation: str,
        zone_id: str,
        context: dict[str, Any] | None = None,
    ) -> list[Entity]:
        """Find all objects related to obj via relation (zone-scoped).

        For parent inheritance: (child, relation, parent) - returns parents.

        Args:
            obj: Object entity
            relation: Relation type
            zone_id: Zone ID to scope the query

        Returns:
            List of related object entities within the zone
        """
        logger.debug(f"find_related_objects: obj={obj}, relation={relation}, zone_id={zone_id}")

        # For parent relation on files, compute from path instead of querying DB
        if relation == "parent" and obj.entity_type == "file":
            parent_path = str(PurePosixPath(obj.entity_id).parent)
            if parent_path != obj.entity_id and parent_path != ".":
                logger.debug(
                    f"find_related_objects: Computed parent from path: {obj.entity_id} -> {parent_path}"
                )
                return [Entity("file", parent_path)]
            return []

        now = datetime.now(UTC)
        stmt = select(RT.object_type, RT.object_id, RT.conditions).where(
            RT.subject_type == obj.entity_type,
            RT.subject_id == obj.entity_id,
            RT.relation == relation,
            RT.zone_id == zone_id,
            or_(RT.expires_at.is_(None), RT.expires_at >= now),
        )

        with self._engine.connect() as conn:
            results = [
                Entity(row.object_type, row.object_id)
                for row in conn.execute(stmt)
                if self._conditions_allow(row.conditions, context)
            ]

            logger.debug(
                f"find_related_objects: Found {len(results)} objects for {obj} via '{relation}': {[str(r) for r in results]}"
            )
            return results

    def find_subjects(
        self,
        obj: Entity,
        relation: str,
        zone_id: str,
        context: dict[str, Any] | None = None,
    ) -> list[Entity]:
        """Find all subjects that have a relation to obj (zone-scoped).

        For group-style tupleToUserset: (subject, relation, obj) - returns subjects.

        Args:
            obj: Object entity (the object in the tuple)
            relation: Relation type (e.g., "direct_viewer")
            zone_id: Zone ID to scope the query

        Returns:
            List of subject entities (the subjects from matching tuples)
        """
        logger.debug(f"find_subjects: Looking for (?, '{relation}', {obj})")

        now = datetime.now(UTC)
        stmt = select(RT.subject_type, RT.subject_id, RT.conditions).where(
            RT.object_type == obj.entity_type,
            RT.object_id == obj.entity_id,
            RT.relation == relation,
            RT.zone_id == zone_id,
            or_(RT.expires_at.is_(None), RT.expires_at >= now),
        )

        with self._engine.connect() as conn:
            results = [
                Entity(row.subject_type, row.subject_id)
                for row in conn.execute(stmt)
                if self._conditions_allow(row.conditions, context)
            ]

            logger.debug(
                f"find_subjects: Found {len(results)} subjects for (?, '{relation}', {obj}): {[str(r) for r in results]}"
            )
            return results
