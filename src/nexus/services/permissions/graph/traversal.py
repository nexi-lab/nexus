"""Permission graph traversal engine.

Issue #1459 Phase 8: Extracts the core Zanzibar-style graph traversal
algorithms from ReBACManager into a focused module.

Contains:
- PermissionComputer: Core permission check via graph traversal
  (compute_permission, has_direct_relation, find_direct_relation_tuple)
- Permission explanation with path tracking
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.rebac import WILDCARD_SUBJECT, Entity

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.core.rebac import NamespaceConfig
    from nexus.services.permissions.tuples.repository import TupleRepository

logger = logging.getLogger(__name__)


class PermissionComputer:
    """Computes permissions via Zanzibar-style graph traversal.

    Responsibilities:
        - Graph traversal with cycle detection and depth limits
        - Direct relation tuple lookup (concrete, wildcard, userset-as-subject)
        - Permission expansion via namespace config (union, intersection,
          exclusion, tupleToUserset)
        - ABAC condition evaluation
        - Permission explanation with path tracking

    Args:
        repo: TupleRepository for database access
        namespace_resolver: Callable that returns NamespaceConfig for a type
        max_depth: Maximum graph traversal depth (default 10)
    """

    def __init__(
        self,
        repo: TupleRepository,
        namespace_resolver: Callable[[str], NamespaceConfig | None],
        max_depth: int = 10,
    ) -> None:
        self._repo = repo
        self._namespace_resolver = namespace_resolver
        self._max_depth = max_depth

    @property
    def max_depth(self) -> int:
        return self._max_depth

    # ------------------------------------------------------------------
    # Core graph traversal
    # ------------------------------------------------------------------

    def compute_permission(
        self,
        subject: Entity,
        permission: str | dict[str, Any],
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Compute permission via graph traversal.

        Args:
            subject: Subject entity
            permission: Permission to check (can be string or userset dict)
            obj: Object entity
            visited: Set of visited (subject_type, subject_id, permission,
                     object_type, object_id) to detect cycles
            depth: Current traversal depth
            context: Optional ABAC context for condition evaluation
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            True if permission is granted
        """
        # P0-6: Explicit deny on graph traversal limit exceeded
        # Security policy: ALWAYS deny when graph is too deep (never allow)
        if depth > self._max_depth:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "ReBAC graph traversal depth limit exceeded (max=%d): "
                    "DENYING permission '%s' for %s -> %s",
                    self._max_depth,
                    permission,
                    subject,
                    obj,
                )
            return False  # EXPLICIT DENY - never allow on limit exceed

        # P0-6: Check for cycles (prevent infinite loops)
        # Convert permission to hashable string for visit_key
        permission_key = (
            json.dumps(permission, sort_keys=True) if isinstance(permission, dict) else permission
        )
        visit_key = (
            subject.entity_type,
            subject.entity_id,
            permission_key,
            obj.entity_type,
            obj.entity_id,
        )
        if visit_key in visited:
            # Cycle detected - deny to prevent infinite loop
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "ReBAC graph cycle detected: DENYING permission '%s' "
                    "for %s -> %s (already visited)",
                    permission,
                    subject,
                    obj,
                )
            return False  # EXPLICIT DENY - never allow cycles
        visited.add(visit_key)

        # Handle dict permission (userset rewrite rules from Zanzibar)
        if isinstance(permission, dict):
            return self._compute_dict_permission(
                subject, permission, obj, visited, depth, context, zone_id
            )

        # Get namespace config for object type
        namespace = self._namespace_resolver(obj.entity_type)
        if not namespace:
            # No namespace config - check for direct relation only
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  [depth=%d] No namespace for %s, checking direct relation",
                    depth,
                    obj.entity_type,
                )
            return self.has_direct_relation(subject, permission, obj, context, zone_id)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("  [depth=%d] Found namespace for %s", depth, obj.entity_type)

        # P0-1: Use explicit permission-to-userset mapping (Zanzibar-style)
        # Check if permission is defined via "permissions" config (new way)
        if namespace.has_permission(permission):
            return self._check_permission_usersets(
                subject, permission, obj, namespace, visited, depth, context, zone_id
            )

        # Fallback: Check if permission is defined as a relation (legacy)
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined in namespace - check for direct relation
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  [depth=%d] No relation config for '%s', checking direct relation",
                    depth,
                    permission,
                )
            return self.has_direct_relation(subject, permission, obj, context, zone_id)

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            return self._check_union(
                subject, permission, obj, namespace, visited, depth, context, zone_id
            )

        # Handle intersection (AND of multiple relations)
        if namespace.has_intersection(permission):
            intersection_relations = namespace.get_intersection_relations(permission)
            # ALL relations must be true
            for rel in intersection_relations:
                if not self.compute_permission(
                    subject, rel, obj, visited.copy(), depth + 1, context, zone_id
                ):
                    return False  # If any relation is False, whole intersection is False
            return True  # All relations were True

        # Handle exclusion (NOT relation - this implements DENY semantics)
        if namespace.has_exclusion(permission):
            excluded_rel = namespace.get_exclusion_relation(permission)
            if excluded_rel:
                # Must NOT have the excluded relation
                return not self.compute_permission(
                    subject, excluded_rel, obj, visited.copy(), depth + 1, context, zone_id
                )
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            return self._check_tuple_to_userset(
                subject, permission, obj, namespace, visited, depth, context, zone_id
            )

        # Direct relation check (with optional context evaluation)
        return self.has_direct_relation(subject, permission, obj, context, zone_id)

    def _compute_dict_permission(
        self,
        subject: Entity,
        permission: dict[str, Any],
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None,
        zone_id: str | None,
    ) -> bool:
        """Handle dict permission (userset rewrite rules from Zanzibar)."""
        # Handle "this" - direct relation check
        if "this" in permission:
            return False

        # Handle "computed_userset" - check a specific relation on the same object
        if "computed_userset" in permission:
            computed = permission["computed_userset"]
            if isinstance(computed, dict):
                relation_name = computed.get("relation")
                if relation_name:
                    return self.compute_permission(
                        subject,
                        relation_name,
                        obj,
                        visited.copy(),
                        depth + 1,
                        context,
                        zone_id,
                    )
            return False

        # Unknown dict format - deny
        logger.warning("Unknown permission dict format: %s", permission)
        return False

    def _check_permission_usersets(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None,
        zone_id: str | None,
    ) -> bool:
        """Check permission via usersets defined in namespace."""
        usersets = namespace.get_permission_usersets(permission)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "  [depth=%d] Permission '%s' expands to usersets: %s",
                depth,
                permission,
                usersets,
            )

        for i, userset in enumerate(usersets):
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  [depth=%d] [%d/%d] Checking userset '%s'...",
                    depth,
                    i + 1,
                    len(usersets),
                    userset,
                )
            result = self.compute_permission(
                subject, userset, obj, visited.copy(), depth + 1, context, zone_id
            )
            if result:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "  [depth=%d] [%d/%d] GRANTED via userset '%s'",
                        depth,
                        i + 1,
                        len(usersets),
                        userset,
                    )
                return True
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  [depth=%d] [%d/%d] DENIED for userset '%s'",
                    depth,
                    i + 1,
                    len(usersets),
                    userset,
                )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "  [depth=%d] ALL %d usersets DENIED - permission DENIED",
                depth,
                len(usersets),
            )
        return False

    def _check_union(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None,
        zone_id: str | None,
    ) -> bool:
        """Check permission via union of relations."""
        union_relations = namespace.get_union_relations(permission)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "  [depth=%d] Relation '%s' is UNION of: %s",
                depth,
                permission,
                union_relations,
            )

        for i, rel in enumerate(union_relations):
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  [depth=%d] [%d/%d] Checking union relation '%s'...",
                    depth,
                    i + 1,
                    len(union_relations),
                    rel,
                )
            result = self.compute_permission(
                subject, rel, obj, visited.copy(), depth + 1, context, zone_id
            )
            if result:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "  [depth=%d] [%d/%d] GRANTED via union relation '%s'",
                        depth,
                        i + 1,
                        len(union_relations),
                        rel,
                    )
                return True
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  [depth=%d] [%d/%d] DENIED for union relation '%s'",
                    depth,
                    i + 1,
                    len(union_relations),
                    rel,
                )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("  [depth=%d] ALL union relations DENIED", depth)
        return False

    def _check_tuple_to_userset(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None,
        zone_id: str | None,
    ) -> bool:
        """Check permission via tupleToUserset (indirect relation)."""
        ttu = namespace.get_tuple_to_userset(permission)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("  [depth=%d] tupleToUserset for '%s': %s", depth, permission, ttu)
        if not ttu:
            return False

        tupleset_relation = ttu["tupleset"]
        computed_userset = ttu["computedUserset"]

        # Pattern 1 (parent-style): Find objects where (obj, tupleset_relation, ?)
        related_objects = self._repo.find_related_objects(obj, tupleset_relation)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "  [depth=%d] Pattern 1 (parent): Found %d related objects via tupleset '%s'",
                depth,
                len(related_objects),
                tupleset_relation,
            )

        for related_obj in related_objects:
            if self.compute_permission(
                subject,
                computed_userset,
                related_obj,
                visited.copy(),
                depth + 1,
                context,
                zone_id,
            ):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "  [depth=%d] GRANTED via tupleToUserset (parent pattern) through %s",
                        depth,
                        related_obj,
                    )
                return True

        # Pattern 2 (group-style): Find subjects where (?, tupleset_relation, obj)
        related_subjects = self._repo.find_subjects_with_relation(obj, tupleset_relation)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "  [depth=%d] Pattern 2 (group): Found %d subjects with '%s' on %s",
                depth,
                len(related_subjects),
                tupleset_relation,
                obj,
            )

        for related_subj in related_subjects:
            if self.compute_permission(
                subject,
                computed_userset,
                related_subj,
                visited.copy(),
                depth + 1,
                context,
                zone_id,
            ):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "  [depth=%d] GRANTED via tupleToUserset (group pattern) through %s",
                        depth,
                        related_subj,
                    )
                return True

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "  [depth=%d] tupleToUserset: No related objects/subjects granted permission",
                depth,
            )
        return False

    # ------------------------------------------------------------------
    # Direct relation checks
    # ------------------------------------------------------------------

    def has_direct_relation(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has direct relation to object.

        Checks both:
        1. Direct concrete subject tuple: (subject, relation, object)
        2. Userset-as-subject tuple: (subject_set#set_relation, relation, object)
           where subject has set_relation on subject_set

        If context is provided, evaluates tuple conditions (ABAC).

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            context: Optional ABAC context for condition evaluation
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            True if direct relation exists and conditions are satisfied
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "    Checking DATABASE for direct tuple: subject=%s, "
                "relation=%s, object=%s, zone_id=%s",
                subject,
                relation,
                obj,
                zone_id,
            )
        result = self.find_direct_relation_tuple(subject, relation, obj, context, zone_id)
        if result is not None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("    FOUND tuple: %s", result.get("tuple_id", "unknown"))
            return True
        else:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("    NO tuple found in database")
            return False

    def find_direct_relation_tuple(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find direct relation tuple with full details.

        Performs three checks:
        1. Direct concrete subject match
        2. Wildcard/public access (*:*) including cross-zone wildcards
        3. Userset-as-subject grants (recursive via has_direct_relation)

        Returns tuple information for explain API.

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            context: Optional ABAC context for condition evaluation
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            Tuple dict with id, subject, relation, object info, or None if not found
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "    find_direct_relation_tuple: subject=%s, relation=%s, obj=%s, zone_id=%s",
                subject,
                relation,
                obj,
                zone_id,
            )

        with self._repo.connection() as conn:
            cursor = self._repo.create_cursor(conn)

            # Check 1: Direct concrete subject (subject_relation IS NULL)
            row = self._query_direct_tuple(cursor, subject, relation, obj, zone_id)
            if row:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "    Found direct tuple for %s -> %s -> %s", subject, relation, obj
                    )
                # Evaluate ABAC conditions if present
                result = self._evaluate_tuple_conditions(row, context)
                if result is not None:
                    return result

            # Check 2: Wildcard/public access
            if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
                wildcard_result = self._check_wildcard_access(cursor, relation, obj, zone_id)
                if wildcard_result is not None:
                    return wildcard_result

            # Check 3: Userset-as-subject grants
            return self._check_userset_grants(cursor, subject, relation, obj, context, zone_id)

    def _query_direct_tuple(
        self,
        cursor: Any,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str | None,
    ) -> dict[str, Any] | None:
        """Query for a direct concrete subject tuple."""
        now_iso = datetime.now(UTC).isoformat()
        fix = self._repo.fix_sql_placeholders

        if zone_id is None:
            cursor.execute(
                fix(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id IS NULL
                    LIMIT 1
                    """
                ),
                (
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    now_iso,
                ),
            )
        else:
            cursor.execute(
                fix(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id = ?
                    LIMIT 1
                    """
                ),
                (
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    now_iso,
                    zone_id,
                ),
            )

        row = cursor.fetchone()
        return dict(row) if row else None

    def _evaluate_tuple_conditions(
        self,
        row: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Evaluate ABAC conditions on a tuple row.

        Returns the row dict if conditions pass, None if they fail.
        """
        from nexus.services.permissions.tuples.repository import TupleRepository

        conditions_json = row.get("conditions")
        if conditions_json:
            try:
                conditions = (
                    json.loads(conditions_json)
                    if isinstance(conditions_json, str)
                    else conditions_json
                )
                if not TupleRepository.evaluate_conditions(conditions, context):
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Tuple exists but conditions not satisfied")
                    return None  # Tuple exists but conditions failed
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Failed to parse conditions JSON: %s", e)
                # On parse error, treat as no conditions (allow)

        return row

    def _check_wildcard_access(
        self,
        cursor: Any,
        relation: str,
        obj: Entity,
        zone_id: str | None,
    ) -> dict[str, Any] | None:
        """Check wildcard/public access (*:*) including cross-zone."""
        wildcard_entity = Entity(WILDCARD_SUBJECT[0], WILDCARD_SUBJECT[1])
        now_iso = datetime.now(UTC).isoformat()
        fix = self._repo.fix_sql_placeholders

        if zone_id is None:
            cursor.execute(
                fix(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id IS NULL
                    LIMIT 1
                    """
                ),
                (
                    wildcard_entity.entity_type,
                    wildcard_entity.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    now_iso,
                ),
            )
        else:
            cursor.execute(
                fix(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                      AND zone_id = ?
                    LIMIT 1
                    """
                ),
                (
                    wildcard_entity.entity_type,
                    wildcard_entity.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    now_iso,
                    zone_id,
                ),
            )

        row = cursor.fetchone()
        if row:
            return dict(row)

        # Check 2b: Cross-zone wildcard access (Issue #1064)
        if zone_id is not None:
            cursor.execute(
                fix(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    LIMIT 1
                    """
                ),
                (
                    wildcard_entity.entity_type,
                    wildcard_entity.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    now_iso,
                ),
            )
            row = cursor.fetchone()
            if row:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "    Cross-zone wildcard access: *:* -> %s -> %s",
                        relation,
                        obj,
                    )
                return dict(row)

        return None

    def _check_userset_grants(
        self,
        cursor: Any,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None,
        zone_id: str | None,
    ) -> dict[str, Any] | None:
        """Check userset-as-subject grants (e.g., group:eng#member)."""
        fix = self._repo.fix_sql_placeholders

        subject_sets = self._repo.find_subject_sets(relation, obj, zone_id)
        for set_type, set_id, set_relation in subject_sets:
            # Recursively check if subject has set_relation on the set entity
            if self.has_direct_relation(
                subject, set_relation, Entity(set_type, set_id), context, zone_id
            ):
                # Return the userset tuple that granted access
                cursor.execute(
                    fix(
                        """
                        SELECT tuple_id, subject_type, subject_id, subject_relation,
                               relation, object_type, object_id, conditions, expires_at
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation = ?
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                        LIMIT 1
                        """
                    ),
                    (set_type, set_id, set_relation, relation, obj.entity_type, obj.entity_id),
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)

        return None

    # ------------------------------------------------------------------
    # Explanation / audit API helpers
    # ------------------------------------------------------------------

    def compute_permission_with_explanation(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        paths: list[dict[str, Any]],
        zone_id: str | None = None,
    ) -> bool:
        """Compute permission with detailed path tracking for explanation.

        This is similar to compute_permission but tracks all paths explored.

        Args:
            subject: Subject entity
            permission: Permission to check
            obj: Object entity
            visited: Set of visited nodes to detect cycles
            depth: Current traversal depth
            paths: List to accumulate path information
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            True if permission is granted
        """
        # Initialize path entry
        path_entry: dict[str, Any] = {
            "subject": str(subject),
            "permission": permission,
            "object": str(obj),
            "depth": depth,
            "granted": False,
        }

        # Check depth limit
        if depth > self._max_depth:
            path_entry["error"] = f"Depth limit exceeded (max={self._max_depth})"
            paths.append(path_entry)
            return False

        # Check for cycles
        visit_key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        if visit_key in visited:
            path_entry["error"] = "Cycle detected"
            paths.append(path_entry)
            return False
        visited.add(visit_key)

        # Get namespace config
        namespace = self._namespace_resolver(obj.entity_type)
        if not namespace:
            # No namespace - check direct relation only
            tuple_info = self.find_direct_relation_tuple(subject, permission, obj, zone_id=zone_id)
            direct = tuple_info is not None
            path_entry["direct_relation"] = direct
            if tuple_info:
                path_entry["tuple"] = tuple_info
            path_entry["granted"] = direct
            paths.append(path_entry)
            return direct

        # Check if permission is defined explicitly
        if namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            path_entry["expanded_to"] = usersets

            for userset in usersets:
                userset_sub_paths: list[dict[str, Any]] = []
                if self.compute_permission_with_explanation(
                    subject, userset, obj, visited.copy(), depth + 1, userset_sub_paths, zone_id
                ):
                    path_entry["granted"] = True
                    path_entry["via_userset"] = userset
                    path_entry["sub_paths"] = userset_sub_paths
                    paths.append(path_entry)
                    return True

            paths.append(path_entry)
            return False

        # Check if permission is defined as a relation (legacy)
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Not defined in namespace - check direct relation
            tuple_info = self.find_direct_relation_tuple(subject, permission, obj, zone_id=zone_id)
            direct = tuple_info is not None
            path_entry["direct_relation"] = direct
            if tuple_info:
                path_entry["tuple"] = tuple_info
            path_entry["granted"] = direct
            paths.append(path_entry)
            return direct

        # Handle union
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            path_entry["union"] = union_relations

            for rel in union_relations:
                union_sub_paths: list[dict[str, Any]] = []
                if self.compute_permission_with_explanation(
                    subject, rel, obj, visited.copy(), depth + 1, union_sub_paths, zone_id
                ):
                    path_entry["granted"] = True
                    path_entry["via_union_member"] = rel
                    path_entry["sub_paths"] = union_sub_paths
                    paths.append(path_entry)
                    return True

            paths.append(path_entry)
            return False

        # Handle intersection
        if namespace.has_intersection(permission):
            intersection_relations = namespace.get_intersection_relations(permission)
            path_entry["intersection"] = intersection_relations
            all_granted = True

            for rel in intersection_relations:
                intersection_sub_paths: list[dict[str, Any]] = []
                if not self.compute_permission_with_explanation(
                    subject, rel, obj, visited.copy(), depth + 1, intersection_sub_paths, zone_id
                ):
                    all_granted = False
                    break

            path_entry["granted"] = all_granted
            paths.append(path_entry)
            return all_granted

        # Handle exclusion
        if namespace.has_exclusion(permission):
            excluded_rel = namespace.get_exclusion_relation(permission)
            if excluded_rel:
                exclusion_sub_paths: list[dict[str, Any]] = []
                has_excluded = self.compute_permission_with_explanation(
                    subject,
                    excluded_rel,
                    obj,
                    visited.copy(),
                    depth + 1,
                    exclusion_sub_paths,
                    zone_id,
                )
                path_entry["exclusion"] = excluded_rel
                path_entry["granted"] = not has_excluded
                paths.append(path_entry)
                return not has_excluded

            paths.append(path_entry)
            return False

        # Handle tupleToUserset
        if namespace.has_tuple_to_userset(permission):
            return self._explain_tuple_to_userset(
                subject, permission, obj, namespace, visited, depth, paths, path_entry, zone_id
            )

        # Direct relation check
        tuple_info = self.find_direct_relation_tuple(subject, permission, obj, zone_id=zone_id)
        direct = tuple_info is not None
        path_entry["direct_relation"] = direct
        if tuple_info:
            path_entry["tuple"] = tuple_info
        path_entry["granted"] = direct
        paths.append(path_entry)
        return direct

    def _explain_tuple_to_userset(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        paths: list[dict[str, Any]],
        path_entry: dict[str, Any],
        zone_id: str | None,
    ) -> bool:
        """Handle tupleToUserset explanation."""
        ttu = namespace.get_tuple_to_userset(permission)
        if ttu:
            tupleset_relation = ttu["tupleset"]
            computed_userset = ttu["computedUserset"]

            # Pattern 1 (parent-style)
            related_objects = self._repo.find_related_objects(obj, tupleset_relation)
            # Pattern 2 (group-style)
            related_subjects = self._repo.find_subjects_with_relation(obj, tupleset_relation)

            path_entry["tupleToUserset"] = {
                "tupleset": tupleset_relation,
                "computedUserset": computed_userset,
                "found_parents": [(o.entity_type, o.entity_id) for o in related_objects],
                "found_subjects": [(s.entity_type, s.entity_id) for s in related_subjects],
            }

            # Check parent-style relations
            for related_obj in related_objects:
                ttu_sub_paths: list[dict[str, Any]] = []
                if self.compute_permission_with_explanation(
                    subject,
                    computed_userset,
                    related_obj,
                    visited.copy(),
                    depth + 1,
                    ttu_sub_paths,
                    zone_id,
                ):
                    path_entry["granted"] = True
                    path_entry["sub_paths"] = ttu_sub_paths
                    path_entry["pattern"] = "parent"
                    paths.append(path_entry)
                    return True

            # Check group-style relations
            for related_subj in related_subjects:
                ttu_sub_paths = []
                if self.compute_permission_with_explanation(
                    subject,
                    computed_userset,
                    related_subj,
                    visited.copy(),
                    depth + 1,
                    ttu_sub_paths,
                    zone_id,
                ):
                    path_entry["granted"] = True
                    path_entry["sub_paths"] = ttu_sub_paths
                    path_entry["pattern"] = "group"
                    paths.append(path_entry)
                    return True

        paths.append(path_entry)
        return False

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def format_path_reason(
        subject: Entity, permission: str, obj: Entity, path: dict[str, Any]
    ) -> str:
        """Format a permission path into a human-readable reason.

        Args:
            subject: Subject entity
            permission: Permission checked
            obj: Object entity
            path: Path dictionary from compute_permission_with_explanation

        Returns:
            Human-readable explanation string
        """
        parts = [f"{subject} has '{permission}' on {obj}"]

        # Extract key information from path
        if "expanded_to" in path:
            relations = path["expanded_to"]
            if relations:
                parts.append(f"(expanded to relations: {', '.join(relations)})")

        if "direct_relation" in path and path["direct_relation"]:
            parts.append("via direct relation")
        elif "tupleToUserset" in path:
            ttu = path["tupleToUserset"]
            if "found_parents" in ttu and ttu["found_parents"]:
                parent = ttu["found_parents"][0]
                parts.append(f"via parent {parent[0]}:{parent[1]}")
        elif "union" in path:
            parts.append("via union of relations")

        return " ".join(parts)
