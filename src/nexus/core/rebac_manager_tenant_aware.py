"""
Tenant-Aware ReBAC Manager (P0-2 Implementation)

This module extends ReBACManager with tenant isolation to prevent
cross-tenant graph traversal.

CRITICAL SECURITY FIX: Enforces same-tenant relationships at write time
and filters all queries by tenant_id.

Usage:
    from nexus.core.rebac_manager_tenant_aware import TenantAwareReBACManager

    manager = TenantAwareReBACManager(engine)

    # All operations now require tenant_id
    manager.rebac_write(
        subject=("user", "alice"),
        relation="editor",
        object=("file", "/workspace/doc.txt"),
        tenant_id="org_acme",  # REQUIRED
    )

Migration Path:
    1. Run migrations/add_tenant_id_to_rebac_tuples.py
    2. Replace ReBACManager with TenantAwareReBACManager
    3. Update all rebac_write/check calls to include tenant_id
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from nexus.core.rebac import CROSS_TENANT_ALLOWED_RELATIONS, Entity, NamespaceConfig
from nexus.core.rebac_manager import ReBACManager

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class TenantIsolationError(Exception):
    """Raised when attempting cross-tenant operations."""

    def __init__(self, message: str, subject_tenant: str | None, object_tenant: str | None):
        super().__init__(message)
        self.subject_tenant = subject_tenant
        self.object_tenant = object_tenant


class TenantAwareReBACManager(ReBACManager):
    """ReBAC Manager with tenant isolation enforcement.

    Extends ReBACManager to:
    1. Require tenant_id on all write operations
    2. Enforce same-tenant relationships
    3. Filter all queries by tenant_id
    4. Prevent cross-tenant graph traversal

    Security Guarantees:
    - Tuples can only link entities within the same tenant
    - Permission checks are scoped to single tenant
    - Graph traversal cannot cross tenant boundaries
    """

    def __init__(
        self,
        engine: Engine,
        cache_ttl_seconds: int = 300,
        max_depth: int = 50,
        enforce_tenant_isolation: bool = True,  # Kill-switch
    ):
        """Initialize tenant-aware ReBAC manager.

        Args:
            engine: SQLAlchemy database engine
            cache_ttl_seconds: Cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
            enforce_tenant_isolation: Enable tenant isolation checks (default: True)
        """
        super().__init__(engine, cache_ttl_seconds, max_depth)
        self.enforce_tenant_isolation = enforce_tenant_isolation

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],  # P0 FIX: Support userset-as-subject
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        tenant_id: str | None = None,  # Optional for backward compatibility (defaults to "default")
        subject_tenant_id: str | None = None,  # Optional: override subject tenant
        object_tenant_id: str | None = None,  # Optional: override object tenant
    ) -> str:
        """Create a relationship tuple with tenant isolation.

        P0 FIX: Now supports userset-as-subject (3-tuple) for group permissions.

        Args:
            subject: (subject_type, subject_id) or (subject_type, subject_id, subject_relation) tuple
                    For userset-as-subject: ("group", "eng", "member") means "all members of group eng"
            relation: Relation type (e.g., 'member-of', 'owner-of')
            object: (object_type, object_id) tuple
            tenant_id: Tenant ID for this relationship (REQUIRED)
            expires_at: Optional expiration time
            conditions: Optional JSON conditions
            subject_tenant_id: Subject's tenant (defaults to tenant_id)
            object_tenant_id: Object's tenant (defaults to tenant_id)

        Returns:
            Tuple ID of created relationship

        Raises:
            TenantIsolationError: If subject and object are in different tenants
            ValueError: If tenant_id is None or empty

        Example:
            >>> # Direct subject
            >>> manager.rebac_write(
            ...     subject=("user", "alice"),
            ...     relation="editor",
            ...     object=("file", "/workspace/doc.txt"),
            ...     tenant_id="org_acme",
            ... )
            >>> # Userset-as-subject (group members)
            >>> manager.rebac_write(
            ...     subject=("group", "engineering", "member"),
            ...     relation="direct_owner",
            ...     object=("file", "/project.txt"),
            ...     tenant_id="org_acme",
            ... )
        """
        # Ensure default namespaces are initialized
        self._ensure_namespaces_initialized()

        # If tenant isolation is disabled, use base ReBACManager implementation
        if not self.enforce_tenant_isolation:
            # Call the base ReBACManager.rebac_write directly (without tenant enforcement)
            return ReBACManager.rebac_write(
                self,
                subject=subject,
                relation=relation,
                object=object,
                expires_at=expires_at,
                conditions=conditions,
                tenant_id=tenant_id,
                subject_tenant_id=subject_tenant_id,
                object_tenant_id=object_tenant_id,
            )

        # Default tenant_id for backward compatibility
        if not tenant_id:
            tenant_id = "default"

        # Default subject/object tenant to main tenant_id
        subject_tenant_id = subject_tenant_id or tenant_id
        object_tenant_id = object_tenant_id or tenant_id

        # Check if this relation is allowed to cross tenant boundaries
        is_cross_tenant_allowed = relation in CROSS_TENANT_ALLOWED_RELATIONS

        # Enforce same-tenant isolation (unless cross-tenant is explicitly allowed)
        if subject_tenant_id != object_tenant_id:
            if is_cross_tenant_allowed:
                # For cross-tenant relations, store with the object's tenant (resource owner)
                # This ensures the share is visible when querying the resource owner's tenant
                tenant_id = object_tenant_id
                logger.info(
                    f"Cross-tenant share: {subject_tenant_id} -> {object_tenant_id} "
                    f"(relation={relation}, stored in tenant={tenant_id})"
                )
            else:
                raise TenantIsolationError(
                    f"Cannot create cross-tenant relationship: "
                    f"subject in {subject_tenant_id}, object in {object_tenant_id}",
                    subject_tenant_id,
                    object_tenant_id,
                )
        if subject_tenant_id != tenant_id and not is_cross_tenant_allowed:
            raise TenantIsolationError(
                f"Subject tenant {subject_tenant_id} does not match tuple tenant {tenant_id}",
                subject_tenant_id,
                tenant_id,
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

        # Create tuple with tenant isolation
        tuple_id = str(uuid.uuid4())
        object_entity = Entity(object[0], object[1])

        with self._connection() as conn:
            # CYCLE DETECTION: Prevent cycles in parent relations
            # Must check BEFORE creating tuple to avoid infinite loops
            if relation == "parent" and self._would_create_cycle_with_conn(
                conn, subject_entity, object_entity, tenant_id
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
                    AND (tenant_id = ? OR (tenant_id IS NULL AND ? IS NULL))
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
                    tenant_id,
                    tenant_id,
                ),
            )
            existing = cursor.fetchone()
            if existing:
                # Tuple already exists, return existing ID (idempotent)
                return cast(
                    str, existing[0] if isinstance(existing, tuple) else existing["tuple_id"]
                )

            # Insert tuple with tenant_id columns (includes subject_relation for userset support)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_tuples (
                        tuple_id, tenant_id, subject_type, subject_id, subject_relation, subject_tenant_id,
                        relation, object_type, object_id, object_tenant_id,
                        created_at, expires_at, conditions
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    tuple_id,
                    tenant_id,
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    subject_relation,  # P0 FIX: Use actual subject_relation for userset-as-subject support
                    subject_tenant_id,
                    relation,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    object_tenant_id,
                    datetime.now(UTC).isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    json.dumps(conditions) if conditions else None,
                ),
            )

            # Log to changelog (include tenant_id)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    INSERT INTO rebac_changelog (
                        change_type, tuple_id, tenant_id, subject_type, subject_id,
                        relation, object_type, object_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    "INSERT",
                    tuple_id,
                    tenant_id,
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
            self._invalidate_cache_for_tuple(
                subject_entity, relation, object_entity, tenant_id, subject_relation, expires_at
            )

            # CROSS-TENANT FIX: If subject is from a different tenant, also invalidate
            # cache for the subject's tenant. This is critical for cross-tenant shares
            # where the permission is granted in resource tenant but checked from user tenant.
            if subject_tenant_id != tenant_id:
                self._invalidate_cache_for_tuple(
                    subject_entity,
                    relation,
                    object_entity,
                    subject_tenant_id,
                    subject_relation,
                    expires_at,
                )

        return tuple_id

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        tenant_id: str | None = None,  # Optional for backward compatibility (defaults to "default")
    ) -> bool:
        """Check if subject has permission on object (tenant-scoped).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            tenant_id: Tenant ID to scope check (REQUIRED)

        Returns:
            True if permission is granted within tenant, False otherwise

        Example:
            >>> manager.rebac_check(
            ...     subject=("user", "alice"),
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     tenant_id="org_acme",
            ... )
            True
        """
        # If tenant isolation is disabled, use base ReBACManager implementation
        if not self.enforce_tenant_isolation:
            # Call the base ReBACManager.rebac_check (without tenant_id)
            return ReBACManager.rebac_check(self, subject, permission, object, context)

        # Default tenant_id for backward compatibility
        if not tenant_id:
            tenant_id = "default"

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Check cache first (include tenant_id in cache key)
        cached = self._get_cached_check_tenant_aware(
            subject_entity, permission, object_entity, tenant_id
        )
        if cached is not None:
            return cached

        # Compute permission via graph traversal (tenant-scoped)
        result = self._compute_permission_tenant_aware(
            subject_entity, permission, object_entity, tenant_id, visited=set(), depth=0
        )

        # Cache result (include tenant_id in cache key)
        self._cache_check_result_tenant_aware(
            subject_entity, permission, object_entity, tenant_id, result
        )

        return result

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        tenant_id: str | None = None,  # Optional for backward compatibility (defaults to "default")
    ) -> list[tuple[str, str]]:
        """Find all subjects with permission on object (tenant-scoped).

        Args:
            permission: Permission to check
            object: (object_type, object_id) tuple
            tenant_id: Tenant ID to scope expansion (REQUIRED)

        Returns:
            List of (subject_type, subject_id) tuples within tenant

        Example:
            >>> manager.rebac_expand(
            ...     permission="read",
            ...     object=("file", "/workspace/doc.txt"),
            ...     tenant_id="org_acme",
            ... )
            [("user", "alice"), ("user", "bob"), ("group", "engineering")]
        """
        # If tenant isolation is disabled, use base ReBACManager implementation
        if not self.enforce_tenant_isolation:
            # Call the base ReBACManager.rebac_expand (without tenant_id)
            return ReBACManager.rebac_expand(self, permission, object)

        # Default tenant_id for backward compatibility
        if not tenant_id:
            tenant_id = "default"

        object_entity = Entity(object[0], object[1])
        subjects: set[tuple[str, str]] = set()

        # Get namespace config
        namespace = self.get_namespace(object_entity.entity_type)
        if not namespace:
            # No namespace - return direct relations only (tenant-scoped)
            return self._get_direct_subjects_tenant_aware(permission, object_entity, tenant_id)

        # Recursively expand permission via namespace config (tenant-scoped)
        self._expand_permission_tenant_aware(
            permission, object_entity, namespace, tenant_id, subjects, visited=set(), depth=0
        )

        return list(subjects)

    # Tenant-aware internal methods

    def _compute_permission_tenant_aware(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tenant_id: str,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
    ) -> bool:
        """Compute permission via graph traversal (tenant-scoped)."""
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
            return self._has_direct_relation_tenant_aware(subject, permission, obj, tenant_id)

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
                if self._compute_permission_tenant_aware(
                    subject, userset, obj, tenant_id, visited.copy(), depth + 1
                ):
                    logger.debug(f"  [depth={depth}] ✅ GRANTED via userset '{userset}'")
                    return True
                else:
                    logger.debug(f"  [depth={depth}] ❌ DENIED via userset '{userset}'")
            logger.debug(f"  [depth={depth}] ❌ No usersets granted permission '{permission}'")
            return False

        # Fallback: Check if permission is defined as a relation (legacy)
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined - check for direct relation
            return self._has_direct_relation_tenant_aware(subject, permission, obj, tenant_id)

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                if self._compute_permission_tenant_aware(
                    subject, rel, obj, tenant_id, visited.copy(), depth + 1
                ):
                    return True
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all objects related via tupleset (tenant-scoped)
                related_objects = self._find_related_objects_tenant_aware(
                    obj, tupleset_relation, tenant_id
                )
                logger.debug(
                    f"  [depth={depth}] tupleToUserset: {permission} - found {len(related_objects)} related objects via '{tupleset_relation}': {[str(o) for o in related_objects]}"
                )

                # Check if subject has computed_userset on any related object
                for related_obj in related_objects:
                    logger.debug(
                        f"  [depth={depth}] Checking if {subject} has '{computed_userset}' on {related_obj}"
                    )
                    if self._compute_permission_tenant_aware(
                        subject, computed_userset, related_obj, tenant_id, visited.copy(), depth + 1
                    ):
                        logger.debug(f"  [depth={depth}] ✅ GRANTED via tupleToUserset")
                        return True
                    else:
                        logger.debug(f"  [depth={depth}] ❌ DENIED for this related object")

                logger.debug(f"  [depth={depth}] ❌ No related objects granted access")

            return False

        # Direct relation check
        return self._has_direct_relation_tenant_aware(subject, permission, obj, tenant_id)

    def _has_direct_relation_tenant_aware(
        self, subject: Entity, relation: str, obj: Entity, tenant_id: str
    ) -> bool:
        """Check if subject has direct relation to object (tenant-scoped).

        P0 SECURITY FIX: Now properly checks userset-as-subject tuples with tenant filtering.
        This prevents cross-tenant group membership leaks.

        Checks three types of relationships:
        1. Direct concrete subject: (alice, editor-of, file:readme)
        2. Wildcard/public access: (*, *, file:readme)
        3. Userset-as-subject: (group:eng#member, editor-of, file:readme)
           where subject has 'member' relation to 'group:eng' (WITHIN SAME TENANT)
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Check 1: Direct concrete subject (subject_relation IS NULL)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT COUNT(*) as count
                    FROM rebac_tuples
                    WHERE tenant_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND subject_relation IS NULL
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    tenant_id,
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
                        WHERE tenant_id = ?
                          AND subject_type = ? AND subject_id = ?
                          AND subject_relation IS NULL
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND (expires_at IS NULL OR expires_at >= ?)
                        """
                    ),
                    (
                        tenant_id,
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

            # Check 2.5: Cross-tenant shares (PR #647)
            # For shared-* relations, check WITHOUT tenant_id filter because
            # cross-tenant shares are stored in the resource owner's tenant
            # but should be visible from the recipient's tenant.
            from nexus.core.rebac import CROSS_TENANT_ALLOWED_RELATIONS

            if relation in CROSS_TENANT_ALLOWED_RELATIONS:
                # Check for cross-tenant share tuples (no tenant filter)
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
                    logger.debug(f"Cross-tenant share found: {subject} -> {relation} -> {obj}")
                    return True

            # Check 3: Userset-as-subject grants (P0 SECURITY FIX!)
            # Find tuples like (group:eng#member, editor-of, file:readme)
            # where subject has 'member' relation to 'group:eng'
            # CRITICAL: This now filters by tenant_id to prevent cross-tenant leaks
            subject_sets = self._find_subject_sets_tenant_aware(relation, obj, tenant_id)
            for set_type, set_id, set_relation in subject_sets:
                # Recursively check if subject has set_relation on the set entity
                # Use tenant-aware check to ensure we stay within the same tenant
                if self._has_direct_relation_tenant_aware(
                    subject, set_relation, Entity(set_type, set_id), tenant_id
                ):
                    return True

            return False

    def _find_subject_sets_tenant_aware(
        self, relation: str, obj: Entity, tenant_id: str
    ) -> list[tuple[str, str, str]]:
        """Find all subject sets that have a relation to an object (tenant-scoped).

        P0 SECURITY FIX: Tenant-aware version of _find_subject_sets.
        Only returns subject sets within the specified tenant.

        Subject sets are tuples with subject_relation set, like:
        (group:eng#member, editor-of, file:readme)

        This means "all members of group:eng have editor-of relation to file:readme"

        Args:
            relation: Relation type
            obj: Object entity
            tenant_id: Tenant ID for isolation

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
                    WHERE tenant_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND subject_relation IS NOT NULL
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    tenant_id,
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

    def _find_related_objects_tenant_aware(
        self, obj: Entity, relation: str, tenant_id: str
    ) -> list[Entity]:
        """Find all objects related to obj via relation (tenant-scoped)."""
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
                    WHERE tenant_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                (
                    tenant_id,
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

    def _get_direct_subjects_tenant_aware(
        self, relation: str, obj: Entity, tenant_id: str
    ) -> list[tuple[str, str]]:
        """Get all subjects with direct relation to object (tenant-scoped)."""
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id
                    FROM rebac_tuples
                    WHERE tenant_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                (
                    tenant_id,
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

    def _expand_permission_tenant_aware(
        self,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        tenant_id: str,
        subjects: set[tuple[str, str]],
        visited: set[tuple[str, str, str]],
        depth: int,
    ) -> None:
        """Recursively expand permission to find all subjects (tenant-scoped)."""
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
            direct_subjects = self._get_direct_subjects_tenant_aware(permission, obj, tenant_id)
            for subj in direct_subjects:
                subjects.add(subj)
            return

        # Handle union
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                self._expand_permission_tenant_aware(
                    rel, obj, namespace, tenant_id, subjects, visited.copy(), depth + 1
                )
            return

        # Handle tupleToUserset
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all related objects
                related_objects = self._find_related_objects_tenant_aware(
                    obj, tupleset_relation, tenant_id
                )

                # Expand permission on related objects
                for related_obj in related_objects:
                    related_ns = self.get_namespace(related_obj.entity_type)
                    if related_ns:
                        self._expand_permission_tenant_aware(
                            computed_userset,
                            related_obj,
                            related_ns,
                            tenant_id,
                            subjects,
                            visited.copy(),
                            depth + 1,
                        )
            return

        # Direct relation - add all subjects
        direct_subjects = self._get_direct_subjects_tenant_aware(permission, obj, tenant_id)
        for subj in direct_subjects:
            subjects.add(subj)

    def _get_cached_check_tenant_aware(
        self, subject: Entity, permission: str, obj: Entity, tenant_id: str
    ) -> bool | None:
        """Get cached permission check result (tenant-aware cache key)."""
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT result, expires_at
                    FROM rebac_check_cache
                    WHERE tenant_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                      AND expires_at > ?
                    """
                ),
                (
                    tenant_id,
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

    def _cache_check_result_tenant_aware(
        self, subject: Entity, permission: str, obj: Entity, tenant_id: str, result: bool
    ) -> None:
        """Cache permission check result (tenant-aware cache key)."""
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
                    WHERE tenant_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                    """
                ),
                (
                    tenant_id,
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
                        cache_id, tenant_id, subject_type, subject_id, permission,
                        object_type, object_id, result, computed_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    cache_id,
                    tenant_id,
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
