"""
Zone-Aware ReBAC Manager (P0-2 Implementation)

This module extends ReBACManager with zone isolation to prevent
cross-zone graph traversal.

CRITICAL SECURITY FIX: Enforces same-zone relationships at write time
and filters all queries by zone_id.

Usage:
    from nexus.core.rebac_manager_zone_aware import ZoneAwareReBACManager

    manager = ZoneAwareReBACManager(engine)

    # All operations now require zone_id
    manager.rebac_write(
        subject=("user", "alice"),
        relation="editor",
        object=("file", "/workspace/doc.txt"),
        zone_id="org_acme",  # REQUIRED
    )

Migration Path:
    1. Run migrations/add_zone_id_to_rebac_tuples.py
    2. Replace ReBACManager with ZoneAwareReBACManager
    3. Update all rebac_write/check calls to include zone_id
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS, Entity, NamespaceConfig
from nexus.core.rebac_manager import ReBACManager

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class ZoneIsolationError(Exception):
    """Raised when attempting cross-zone operations."""

    def __init__(self, message: str, subject_zone: str | None, object_zone: str | None):
        super().__init__(message)
        self.subject_zone = subject_zone
        self.object_zone = object_zone


class ZoneAwareReBACManager(ReBACManager):
    """ReBAC Manager with zone isolation enforcement.

    Extends ReBACManager to:
    1. Require zone_id on all write operations
    2. Enforce same-zone relationships
    3. Filter all queries by zone_id
    4. Prevent cross-zone graph traversal

    Security Guarantees:
    - Tuples can only link entities within the same zone
    - Permission checks are scoped to single zone
    - Graph traversal cannot cross zone boundaries
    """

    def __init__(
        self,
        engine: Engine,
        cache_ttl_seconds: int = 300,
        max_depth: int = 50,
        enforce_zone_isolation: bool = True,  # Kill-switch
    ):
        """Initialize zone-aware ReBAC manager.

        Args:
            engine: SQLAlchemy database engine
            cache_ttl_seconds: Cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
            enforce_zone_isolation: Enable zone isolation checks (default: True)
        """
        super().__init__(engine, cache_ttl_seconds, max_depth)
        self.enforce_zone_isolation = enforce_zone_isolation

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],  # P0 FIX: Support userset-as-subject
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,  # Issue #773: Defaults to "default" internally
        subject_zone_id: str | None = None,  # Optional: override subject zone
        object_zone_id: str | None = None,  # Optional: override object zone
    ) -> str:
        """Create a relationship tuple with zone isolation.

        P0 FIX: Now supports userset-as-subject (3-tuple) for group permissions.

        Args:
            subject: (subject_type, subject_id) or (subject_type, subject_id, subject_relation) tuple
                    For userset-as-subject: ("group", "eng", "member") means "all members of group eng"
            relation: Relation type (e.g., 'member-of', 'owner-of')
            object: (object_type, object_id) tuple
            zone_id: Zone ID for this relationship (REQUIRED)
            expires_at: Optional expiration time
            conditions: Optional JSON conditions
            subject_zone_id: Subject's zone (defaults to zone_id)
            object_zone_id: Object's zone (defaults to zone_id)

        Returns:
            Tuple ID of created relationship

        Raises:
            ZoneIsolationError: If subject and object are in different zones
            ValueError: If zone_id is None or empty

        Example:
            >>> # Direct subject
            >>> manager.rebac_write(
            ...     subject=("user", "alice"),
            ...     relation="editor",
            ...     object=("file", "/workspace/doc.txt"),
            ...     zone_id="org_acme",
            ... )
            >>> # Userset-as-subject (group members)
            >>> manager.rebac_write(
            ...     subject=("group", "engineering", "member"),
            ...     relation="direct_owner",
            ...     object=("file", "/project.txt"),
            ...     zone_id="org_acme",
            ... )
        """
        # Ensure default namespaces are initialized
        self._ensure_namespaces_initialized()

        # If zone isolation is disabled, use base ReBACManager implementation
        if not self.enforce_zone_isolation:
            # Call the base ReBACManager.rebac_write directly (without zone enforcement)
            return ReBACManager.rebac_write(
                self,
                subject=subject,
                relation=relation,
                object=object,
                expires_at=expires_at,
                conditions=conditions,
                zone_id=zone_id,
                subject_zone_id=subject_zone_id,
                object_zone_id=object_zone_id,
            )

        # Issue #773: zone_id is now required, but we keep a safe default
        # for backward compatibility with existing code paths
        if not zone_id:
            zone_id = "default"  # Fallback kept for safety during transition

        # Default subject/object zone to main zone_id
        subject_zone_id = subject_zone_id or zone_id
        object_zone_id = object_zone_id or zone_id

        # Check if this relation is allowed to cross zone boundaries
        is_cross_zone_allowed = relation in CROSS_ZONE_ALLOWED_RELATIONS

        # Enforce same-zone isolation (unless cross-zone is explicitly allowed)
        if subject_zone_id != object_zone_id:
            if is_cross_zone_allowed:
                # For cross-zone relations, store with the object's zone (resource owner)
                # This ensures the share is visible when querying the resource owner's zone
                zone_id = object_zone_id
                logger.info(
                    f"Cross-zone share: {subject_zone_id} -> {object_zone_id} "
                    f"(relation={relation}, stored in zone={zone_id})"
                )
            else:
                raise ZoneIsolationError(
                    f"Cannot create cross-zone relationship: "
                    f"subject in {subject_zone_id}, object in {object_zone_id}",
                    subject_zone_id,
                    object_zone_id,
                )
        if subject_zone_id != zone_id and not is_cross_zone_allowed:
            raise ZoneIsolationError(
                f"Subject zone {subject_zone_id} does not match tuple zone {zone_id}",
                subject_zone_id,
                zone_id,
            )

        # Parse subject (support userset-as-subject with 3-tuple) - P0 FIX
        if len(subject) == 3:
            subject_type, subject_id, subject_relation = subject
            subject_entity = Entity(subject_type, subject_id)
        elif len(subject) == 2:
            subject_type, subject_id = subject
            subject_relation = None
            subject_entity = Entity(subject_type, subject_id)
        else:
            raise ValueError(f"subject must be 2-tuple or 3-tuple, got {len(subject)}-tuple")

        # Create tuple with zone isolation
        tuple_id = str(uuid.uuid4())
        object_entity = Entity(object[0], object[1])

        with self._connection() as conn:
            # CYCLE DETECTION: Prevent cycles in parent relations
            # Must check BEFORE creating tuple to avoid infinite loops
            if relation == "parent" and self._would_create_cycle_with_conn(
                conn, subject_entity, object_entity, zone_id
            ):
                raise ValueError(
                    f"Cycle detected: Creating parent relation from "
                    f"{subject_entity.entity_type}:{subject_entity.entity_id} to "
                    f"{object_entity.entity_type}:{object_entity.entity_id} would create a cycle"
                )

            cursor = self._create_cursor(conn)

            # Check if tuple already exists (idempotency fix)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT tuple_id FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                    AND (subject_relation = ? OR (subject_relation IS NULL AND ? IS NULL))
                    AND relation = ?
                    AND object_type = ? AND object_id = ?
                    AND (zone_id = ? OR (zone_id IS NULL AND ? IS NULL))
                    """
                ),
                (
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    subject_relation,
                    subject_relation,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    zone_id,
                    zone_id,
                ),
            )
            existing = cursor.fetchone()
            if existing:
                # Tuple already exists, return existing ID (idempotent)
                return cast(
                    str, existing[0] if isinstance(existing, tuple) else existing["tuple_id"]
                )

            # Insert tuple with zone_id columns (includes subject_relation for userset support)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, zone_id, subject_type, subject_id, subject_relation, subject_zone_id,
                        relation, object_type, object_id, object_zone_id,
                        created_at, expires_at, conditions
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    tuple_id,
                    zone_id,
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    subject_relation,  # P0 FIX: Use actual subject_relation for userset-as-subject support
                    subject_zone_id,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    object_zone_id,
                    datetime.now(UTC).isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    json.dumps(conditions) if conditions else None,
                ),
            )

            # Log to changelog (include zone_id)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, zone_id, subject_type, subject_id,
                        relation, object_type, object_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    "INSERT",
                    tuple_id,
                    zone_id,
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            conn.commit()

            # Invalidate cache entries affected by this change
            # FIX: Pass conn to avoid opening new connection (pool exhaustion)
            self._invalidate_cache_for_tuple(
                subject_entity,
                relation,
                object_entity,
                zone_id,
                subject_relation,
                expires_at,
                conn=conn,
            )

            # CROSS-ZONE FIX: If subject is from a different zone, also invalidate
            # cache for the subject's zone. This is critical for cross-zone shares
            # where the permission is granted in resource zone but checked from user zone.
            if subject_zone_id != zone_id:
                self._invalidate_cache_for_tuple(
                    subject_entity,
                    relation,
                    object_entity,
                    subject_zone_id,
                    subject_relation,
                    expires_at,
                    conn=conn,  # FIX: Reuse connection
                )

        return tuple_id

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,  # Issue #773: Defaults to "default" internally
    ) -> bool:
        """Check if subject has permission on object (zone-scoped).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            zone_id: Zone ID to scope check (REQUIRED)

        Returns:
            True if permission is granted within zone, False otherwise

        Example:
            >>> manager.rebac_check(
            ...     subject=("user", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     zone_id="org_acme",
            ... )
            True
        """
        # If zone isolation is disabled, use base ReBACManager implementation
        if not self.enforce_zone_isolation:
            # Call the base ReBACManager.rebac_check (without zone_id)
            return ReBACManager.rebac_check(self, subject, permission, object, context)

        # Issue #773: zone_id is now required, but we keep a safe default
        # for backward compatibility with existing code paths
        if not zone_id:
            zone_id = "default"  # Fallback kept for safety during transition

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Check cache first (include zone_id in cache key)
        cached = self._get_cached_check_zone_aware(
            subject_entity, permission, object_entity, zone_id
        )
        if cached is not None:
            return cached

        # Compute permission via graph traversal (zone-scoped)
        result = self._compute_permission_zone_aware(
            subject_entity, permission, object_entity, zone_id, visited=set(), depth=0
        )

        # Cache result (include zone_id in cache key)
        self._cache_check_result_zone_aware(
            subject_entity, permission, object_entity, zone_id, result
        )

        return result

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str = "default",  # Issue #773: Required for multi-zone isolation
    ) -> list[tuple[str, str]]:
        """Find all subjects with permission on object (zone-scoped).

        Args:
            permission: Permission to check
            object: (object_type, object_id) tuple
            zone_id: Zone ID to scope expansion (REQUIRED)

        Returns:
            List of (subject_type, subject_id) tuples within zone

        Example:
            >>> manager.rebac_expand(
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     zone_id="org_acme",
            ... )
            [("user", "alice"), ("user", "bob"), ("group", "engineering")]
        """
        # If zone isolation is disabled, use base ReBACManager implementation
        if not self.enforce_zone_isolation:
            # Call the base ReBACManager.rebac_expand (without zone_id)
            return ReBACManager.rebac_expand(self, permission, object)

        # Issue #773: zone_id is now required, but we keep a safe default
        # for backward compatibility with existing code paths
        if not zone_id:
            zone_id = "default"  # Fallback kept for safety during transition

        object_entity = Entity(object[0], object[1])
        subjects: set[tuple[str, str]] = set()

        # Get namespace config
        namespace = self.get_namespace(object_entity.entity_type)
        if not namespace:
            # No namespace - return direct relations only (zone-scoped)
            return self._get_direct_subjects_zone_aware(permission, object_entity, zone_id)

        # Recursively expand permission via namespace config (zone-scoped)
        self._expand_permission_zone_aware(
            permission, object_entity, namespace, zone_id, subjects, visited=set(), depth=0
        )

        return list(subjects)

    # Zone-aware internal methods

    def _compute_permission_zone_aware(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        zone_id: str,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
    ) -> bool:
        """Compute permission via graph traversal (zone-scoped)."""
        # Check depth limit
        if depth > self.max_depth:
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
            return False
        visited.add(visit_key)

        # Get namespace config
        namespace = self.get_namespace(obj.entity_type)
        if not namespace:
            # No namespace - check for direct relation only
            return self._has_direct_relation_zone_aware(subject, permission, obj, zone_id)

        # P0-1: Check if permission is defined via "permissions" config (Zanzibar-style)
        # This must be checked FIRST before checking relations
        if namespace.has_permission(permission):
            # Permission defined explicitly - check all usersets that grant it
            usersets = namespace.get_permission_usersets(permission)
            logger.debug(
                f"  [depth={depth}] Permission '{permission}' expands to usersets: {usersets}"
            )
            for userset in usersets:
                logger.debug(f"  [depth={depth}] Checking userset '{userset}' for {obj}")
                if self._compute_permission_zone_aware(
                    subject, userset, obj, zone_id, visited.copy(), depth + 1
                ):
                    logger.debug(f"  [depth={depth}] GRANTED via userset '{userset}'")
                    return True
                else:
                    logger.debug(f"  [depth={depth}] DENIED via userset '{userset}'")
            logger.debug(f"  [depth={depth}] No usersets granted permission '{permission}'")
            return False

        # Fallback: Check if permission is defined as a relation (legacy)
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined - check for direct relation
            return self._has_direct_relation_zone_aware(subject, permission, obj, zone_id)

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                if self._compute_permission_zone_aware(
                    subject, rel, obj, zone_id, visited.copy(), depth + 1
                ):
                    return True
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all objects related via tupleset (zone-scoped)
                related_objects = self._find_related_objects_zone_aware(
                    obj, tupleset_relation, zone_id
                )
                logger.debug(
                    f"  [depth={depth}] tupleToUserset: {permission} - found {len(related_objects)} related objects via '{tupleset_relation}': {[str(o) for o in related_objects]}"
                )

                # Check if subject has computed_userset on any related object
                for related_obj in related_objects:
                    logger.debug(
                        f"  [depth={depth}] Checking if {subject} has '{computed_userset}' on {related_obj}"
                    )
                    if self._compute_permission_zone_aware(
                        subject, computed_userset, related_obj, zone_id, visited.copy(), depth + 1
                    ):
                        logger.debug(f"  [depth={depth}] GRANTED via tupleToUserset")
                        return True
                    else:
                        logger.debug(f"  [depth={depth}] DENIED for this related object")

                logger.debug(f"  [depth={depth}] No related objects granted access")

            return False

        # Direct relation check
        return self._has_direct_relation_zone_aware(subject, permission, obj, zone_id)

    def _has_direct_relation_zone_aware(
        self, subject: Entity, relation: str, obj: Entity, zone_id: str
    ) -> bool:
        """Check if subject has direct relation to object (zone-scoped).

        P0 SECURITY FIX: Now properly checks userset-as-subject tuples with zone filtering.
        This prevents cross-zone group membership leaks.

        Checks three types of relationships:
        1. Direct concrete subject: (alice, editor-of, file:readme)
        2. Wildcard/public access: (*, *, file:readme)
        3. Userset-as-subject: (group:eng#member, editor-of, file:readme)
           where subject has 'member' relation to 'group:eng' (WITHIN SAME ZONE)
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Check 1: Direct concrete subject (subject_relation IS NULL)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT COUNT(*) as count
                    FROM rebac_tuples
                    WHERE zone_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    zone_id,
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            row = cursor.fetchone()
            count = row["count"]
            if count > 0:
                return True

            # Check 2: Wildcard/public access
            # Check if wildcard subject (*:*) has the relation (public access)
            from nexus.core.rebac import WILDCARD_SUBJECT

            if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
                wildcard_entity = Entity(WILDCARD_SUBJECT[0], WILDCARD_SUBJECT[1])
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT COUNT(*) as count
                        FROM rebac_tuples
                        WHERE zone_id = ?
                          AND subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        zone_id,
                        wildcard_entity.entity_type,
                        wildcard_entity.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                row = cursor.fetchone()
                count = row["count"]
                if count > 0:
                    return True

                # Check 2b: Cross-zone wildcard access (Issue #1064)
                # Wildcards should grant access across ALL zones.
                # This is the industry-standard pattern used by SpiceDB, OpenFGA, Ory Keto.
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT COUNT(*) as count
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        wildcard_entity.entity_type,
                        wildcard_entity.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                row = cursor.fetchone()
                count = row["count"]
                if count > 0:
                    logger.debug(f"Cross-zone wildcard access: *:* -> {relation} -> {obj}")
                    return True

            # Check 2.5: Cross-zone shares (PR #647)
            # For shared-* relations, check WITHOUT zone_id filter because
            # cross-zone shares are stored in the resource owner's zone
            # but should be visible from the recipient's zone.
            from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS

            if relation in CROSS_ZONE_ALLOWED_RELATIONS:
                # Check for cross-zone share tuples (no zone filter)
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT COUNT(*) as count
                        FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        subject.entity_type,
                        subject.entity_id,
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                row = cursor.fetchone()
                count = row["count"]
                if count > 0:
                    logger.debug(f"Cross-zone share found: {subject} -> {relation} -> {obj}")
                    return True

            # Check 3: Userset-as-subject grants (P0 SECURITY FIX!)
            # Find tuples like (group:eng#member, editor-of, file:readme)
            # where subject has 'member' relation to 'group:eng'
            # CRITICAL: This now filters by zone_id to prevent cross-zone leaks
            subject_sets = self._find_subject_sets_zone_aware(relation, obj, zone_id)
            for set_type, set_id, set_relation in subject_sets:
                # Recursively check if subject has set_relation on the set entity
                # Use zone-aware check to ensure we stay within the same zone
                if self._has_direct_relation_zone_aware(
                    subject, set_relation, Entity(set_type, set_id), zone_id
                ):
                    return True

            return False

    def _find_subject_sets_zone_aware(
        self, relation: str, obj: Entity, zone_id: str
    ) -> list[tuple[str, str, str]]:
        """Find all subject sets that have a relation to an object (zone-scoped).

        P0 SECURITY FIX: Zone-aware version of _find_subject_sets.
        Only returns subject sets within the specified zone.

        Subject sets are tuples with subject_relation set, like:
        (group:eng#member, editor-of, file:readme)

        This means "all members of group:eng have editor-of relation to file:readme"

        Args:
            relation: Relation type
            obj: Object entity
            zone_id: Zone ID for isolation

        Returns:
            List of (subject_type, subject_id, subject_relation) tuples
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, subject_relation
                    FROM rebac_tuples
                    WHERE zone_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND subject_relation IS NOT NULL
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    zone_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                results.append((row["subject_type"], row["subject_id"], row["subject_relation"]))
            return results

    def _find_related_objects_zone_aware(
        self, obj: Entity, relation: str, zone_id: str
    ) -> list[Entity]:
        """Find all objects related to obj via relation (zone-scoped)."""
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # FIX: For tupleToUserset, we need to find tuples where obj is the SUBJECT
            # Example: To find parent of file X, look for (X, parent, Y) and return Y
            # NOT (?, ?, X) - that would be finding children!
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT object_type, object_id
                    FROM rebac_tuples
                    WHERE zone_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                (
                    zone_id,
                    obj.entity_type,
                    obj.entity_id,
                    relation,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                results.append(Entity(row["object_type"], row["object_id"]))
            return results

    def _get_direct_subjects_zone_aware(
        self, relation: str, obj: Entity, zone_id: str
    ) -> list[tuple[str, str]]:
        """Get all subjects with direct relation to object (zone-scoped)."""
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id
                    FROM rebac_tuples
                    WHERE zone_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                (
                    zone_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                results.append((row["subject_type"], row["subject_id"]))
            return results

    def _expand_permission_zone_aware(
        self,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        zone_id: str,
        subjects: set[tuple[str, str]],
        visited: set[tuple[str, str, str]],
        depth: int,
    ) -> None:
        """Recursively expand permission to find all subjects (zone-scoped)."""
        # Check depth limit
        if depth > self.max_depth:
            return

        # Check for cycles
        visit_key = (permission, obj.entity_type, obj.entity_id)
        if visit_key in visited:
            return
        visited.add(visit_key)

        # Get relation config
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined - check for direct relations
            direct_subjects = self._get_direct_subjects_zone_aware(permission, obj, zone_id)
            for subj in direct_subjects:
                subjects.add(subj)
            return

        # Handle union
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                self._expand_permission_zone_aware(
                    rel, obj, namespace, zone_id, subjects, visited.copy(), depth + 1
                )
            return

        # Handle tupleToUserset
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all related objects
                related_objects = self._find_related_objects_zone_aware(
                    obj, tupleset_relation, zone_id
                )

                # Expand permission on related objects
                for related_obj in related_objects:
                    related_ns = self.get_namespace(related_obj.entity_type)
                    if related_ns:
                        self._expand_permission_zone_aware(
                            computed_userset,
                            related_obj,
                            related_ns,
                            zone_id,
                            subjects,
                            visited.copy(),
                            depth + 1,
                        )
            return

        # Direct relation - add all subjects
        direct_subjects = self._get_direct_subjects_zone_aware(permission, obj, zone_id)
        for subj in direct_subjects:
            subjects.add(subj)

    def _get_cached_check_zone_aware(
        self, subject: Entity, permission: str, obj: Entity, zone_id: str
    ) -> bool | None:
        """Get cached permission check result (zone-aware cache key)."""
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT result, expires_at
                    FROM rebac_check_cache
                    WHERE zone_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                      AND expires_at > ?
                    """
                ),
                (
                    zone_id,
                    subject.entity_type,
                    subject.entity_id,
                    permission,
                    obj.entity_type,
                    obj.entity_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            row = cursor.fetchone()
            if row:
                result = row["result"]
                return bool(result)
            return None

    def _cache_check_result_zone_aware(
        self, subject: Entity, permission: str, obj: Entity, zone_id: str, result: bool
    ) -> None:
        """Cache permission check result (zone-aware cache key)."""
        cache_id = str(uuid.uuid4())
        computed_at = datetime.now(UTC)
        expires_at = computed_at + timedelta(seconds=self.cache_ttl_seconds)

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Delete existing cache entry if present
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    DELETE FROM rebac_check_cache
                    WHERE zone_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                    """
                ),
                (
                    zone_id,
                    subject.entity_type,
                    subject.entity_id,
                    permission,
                    obj.entity_type,
                    obj.entity_id,
                ),
            )

            # Insert new cache entry
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_check_cache (
                        cache_id, zone_id, subject_type, subject_id, permission,
                        object_type, object_id, result, computed_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    cache_id,
                    zone_id,
                    subject.entity_type,
                    subject.entity_id,
                    permission,
                    obj.entity_type,
                    obj.entity_id,
                    int(result),
                    computed_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )

            conn.commit()
