"""ReBAC Manager for relationship-based access control.

This module implements the core ReBAC APIs:
- Check API: Fast permission checks with graph traversal and caching
- Write API: Create relationship tuples with changelog tracking
- Delete API: Remove relationship tuples with cache invalidation
- Expand API: Find all subjects with a given permission

Based on Google Zanzibar design with optimizations for embedded/local use.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from nexus.core.rebac import (
    DEFAULT_FILE_NAMESPACE,
    DEFAULT_GROUP_NAMESPACE,
    Entity,
    NamespaceConfig,
)


class ReBACManager:
    """Manager for ReBAC operations.

    Provides Zanzibar-style relationship-based access control with:
    - Direct tuple lookup
    - Recursive graph traversal
    - Permission expansion via namespace configs
    - Caching with TTL and invalidation
    - Cycle detection

    Attributes:
        db_path: Path to SQLite database
        cache_ttl_seconds: Time-to-live for cache entries (default: 300 = 5 minutes)
        max_depth: Maximum graph traversal depth (default: 10)
    """

    def __init__(
        self,
        db_path: str,
        cache_ttl_seconds: int = 300,
        max_depth: int = 10,
    ):
        """Initialize ReBAC manager.

        Args:
            db_path: Path to SQLite database
            cache_ttl_seconds: Cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
        """
        self.db_path = db_path
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_depth = max_depth
        self._conn: sqlite3.Connection | None = None
        self._last_cleanup_time: datetime | None = None
        self._ensure_connection()
        self._initialize_default_namespaces()

    def _ensure_connection(self) -> None:
        """Ensure database connection is established."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection."""
        self._ensure_connection()
        assert self._conn is not None
        return self._conn

    def _initialize_default_namespaces(self) -> None:
        """Initialize default namespace configurations if not present."""
        # Check if file namespace exists
        file_ns = self.get_namespace("file")
        if file_ns is None:
            self.create_namespace(DEFAULT_FILE_NAMESPACE)

        # Check if group namespace exists
        group_ns = self.get_namespace("group")
        if group_ns is None:
            self.create_namespace(DEFAULT_GROUP_NAMESPACE)

    def create_namespace(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace configuration.

        Args:
            namespace: Namespace configuration to create
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Check if namespace exists
        cursor.execute(
            "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?",
            (namespace.object_type,),
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing namespace
            cursor.execute(
                """
                UPDATE rebac_namespaces
                SET config = ?, updated_at = ?
                WHERE object_type = ?
                """,
                (
                    json.dumps(namespace.config),
                    datetime.utcnow().isoformat(),
                    namespace.object_type,
                ),
            )
        else:
            # Insert new namespace
            cursor.execute(
                """
                INSERT INTO rebac_namespaces (namespace_id, object_type, config, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    namespace.namespace_id,
                    namespace.object_type,
                    json.dumps(namespace.config),
                    namespace.created_at.isoformat(),
                    namespace.updated_at.isoformat(),
                ),
            )

        conn.commit()

    def get_namespace(self, object_type: str) -> NamespaceConfig | None:
        """Get namespace configuration for an object type.

        Args:
            object_type: Type of object

        Returns:
            NamespaceConfig or None if not found
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT namespace_id, object_type, config, created_at, updated_at
            FROM rebac_namespaces
            WHERE object_type = ?
            """,
            (object_type,),
        )

        row = cursor.fetchone()
        if not row:
            return None

        return NamespaceConfig(
            namespace_id=row["namespace_id"],
            object_type=row["object_type"],
            config=json.loads(row["config"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def rebac_write(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
    ) -> str:
        """Create a relationship tuple.

        Args:
            subject: (subject_type, subject_id) tuple
            relation: Relation type (e.g., 'member-of', 'owner-of')
            object: (object_type, object_id) tuple
            expires_at: Optional expiration time
            conditions: Optional JSON conditions

        Returns:
            Tuple ID of created relationship

        Example:
            >>> manager.rebac_write(
            ...     subject=("agent", "alice_id"),
            ...     relation="member-of",
            ...     object=("group", "eng_team_id")
            ... )
        """
        tuple_id = str(uuid.uuid4())
        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        conn = self._get_connection()
        cursor = conn.cursor()

        # Insert tuple
        cursor.execute(
            """
            INSERT INTO rebac_tuples (
                tuple_id, subject_type, subject_id, relation,
                object_type, object_id, created_at, expires_at, conditions
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tuple_id,
                subject_entity.entity_type,
                subject_entity.entity_id,
                relation,
                object_entity.entity_type,
                object_entity.entity_id,
                datetime.utcnow().isoformat(),
                expires_at.isoformat() if expires_at else None,
                json.dumps(conditions) if conditions else None,
            ),
        )

        # Log to changelog
        cursor.execute(
            """
            INSERT INTO rebac_changelog (
                change_type, tuple_id, subject_type, subject_id,
                relation, object_type, object_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "INSERT",
                tuple_id,
                subject_entity.entity_type,
                subject_entity.entity_id,
                relation,
                object_entity.entity_type,
                object_entity.entity_id,
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()

        # Invalidate cache entries affected by this change
        self._invalidate_cache_for_tuple(subject_entity, relation, object_entity)

        return tuple_id

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple.

        Args:
            tuple_id: ID of tuple to delete

        Returns:
            True if tuple was deleted, False if not found
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Get tuple details before deleting (for changelog)
        cursor.execute(
            """
            SELECT subject_type, subject_id, relation, object_type, object_id
            FROM rebac_tuples
            WHERE tuple_id = ?
            """,
            (tuple_id,),
        )
        row = cursor.fetchone()

        if not row:
            return False

        subject = Entity(row["subject_type"], row["subject_id"])
        relation = row["relation"]
        obj = Entity(row["object_type"], row["object_id"])

        # Delete tuple
        cursor.execute("DELETE FROM rebac_tuples WHERE tuple_id = ?", (tuple_id,))

        # Log to changelog
        cursor.execute(
            """
            INSERT INTO rebac_changelog (
                change_type, tuple_id, subject_type, subject_id,
                relation, object_type, object_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "DELETE",
                tuple_id,
                subject.entity_type,
                subject.entity_id,
                relation,
                obj.entity_type,
                obj.entity_id,
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()

        # Invalidate cache entries affected by this change
        self._invalidate_cache_for_tuple(subject, relation, obj)

        return True

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
    ) -> bool:
        """Check if subject has permission on object.

        Uses caching and recursive graph traversal to compute permissions.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple

        Returns:
            True if permission is granted, False otherwise

        Example:
            >>> manager.rebac_check(
            ...     subject=("agent", "alice_id"),
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            True
        """
        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        # Clean up expired tuples first (this will invalidate affected caches)
        self._cleanup_expired_tuples_if_needed()

        # Check cache first
        cached = self._get_cached_check(subject_entity, permission, object_entity)
        if cached is not None:
            return cached

        # Compute permission via graph traversal
        result = self._compute_permission(
            subject_entity, permission, object_entity, visited=set(), depth=0
        )

        # Cache result
        self._cache_check_result(subject_entity, permission, object_entity, result)

        return result

    def _compute_permission(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
    ) -> bool:
        """Compute permission via graph traversal.

        Args:
            subject: Subject entity
            permission: Permission to check
            obj: Object entity
            visited: Set of visited (subject_type, subject_id, permission, object_type, object_id) to detect cycles
            depth: Current traversal depth

        Returns:
            True if permission is granted
        """
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

        # Get namespace config for object type
        namespace = self.get_namespace(obj.entity_type)
        if not namespace:
            # No namespace config - check for direct relation only
            return self._has_direct_relation(subject, permission, obj)

        # Check if permission is defined in namespace
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            # Permission not defined in namespace - check for direct relation
            return self._has_direct_relation(subject, permission, obj)

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                if self._compute_permission(subject, rel, obj, visited.copy(), depth + 1):
                    return True
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all objects related via tupleset
                related_objects = self._find_related_objects(obj, tupleset_relation)

                # Check if subject has computed_userset on any related object
                for related_obj in related_objects:
                    if self._compute_permission(
                        subject, computed_userset, related_obj, visited.copy(), depth + 1
                    ):
                        return True

            return False

        # Direct relation check
        return self._has_direct_relation(subject, permission, obj)

    def _has_direct_relation(self, subject: Entity, relation: str, obj: Entity) -> bool:
        """Check if subject has direct relation to object.

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity

        Returns:
            True if direct relation exists
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT COUNT(*) as count
            FROM rebac_tuples
            WHERE subject_type = ? AND subject_id = ?
              AND relation = ?
              AND object_type = ? AND object_id = ?
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (
                subject.entity_type,
                subject.entity_id,
                relation,
                obj.entity_type,
                obj.entity_id,
                datetime.utcnow().isoformat(),
            ),
        )

        row = cursor.fetchone()
        return bool(row["count"] > 0) if row else False

    def _find_related_objects(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all objects related to obj via relation.

        Args:
            obj: Object entity
            relation: Relation type

        Returns:
            List of related object entities
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT subject_type, subject_id
            FROM rebac_tuples
            WHERE object_type = ? AND object_id = ?
              AND relation = ?
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (
                obj.entity_type,
                obj.entity_id,
                relation,
                datetime.utcnow().isoformat(),
            ),
        )

        return [Entity(row["subject_type"], row["subject_id"]) for row in cursor.fetchall()]

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
    ) -> list[tuple[str, str]]:
        """Find all subjects with a given permission on an object.

        Args:
            permission: Permission to check
            object: (object_type, object_id) tuple

        Returns:
            List of (subject_type, subject_id) tuples

        Example:
            >>> manager.rebac_expand(
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            [("agent", "alice_id"), ("agent", "bob_id")]
        """
        object_entity = Entity(object[0], object[1])
        subjects: set[tuple[str, str]] = set()

        # Get namespace config
        namespace = self.get_namespace(object_entity.entity_type)
        if not namespace:
            # No namespace - return direct relations only
            return self._get_direct_subjects(permission, object_entity)

        # Recursively expand permission via namespace config
        self._expand_permission(
            permission, object_entity, namespace, subjects, visited=set(), depth=0
        )

        return list(subjects)

    def _expand_permission(
        self,
        permission: str,
        obj: Entity,
        namespace: NamespaceConfig,
        subjects: set[tuple[str, str]],
        visited: set[tuple[str, str, str]],
        depth: int,
    ) -> None:
        """Recursively expand permission to find all subjects.

        Args:
            permission: Permission to expand
            obj: Object entity
            namespace: Namespace configuration
            subjects: Set to accumulate subjects
            visited: Set of visited (permission, object_type, object_id) to detect cycles
            depth: Current traversal depth
        """
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
            # Permission not defined in namespace - check for direct relations
            direct_subjects = self._get_direct_subjects(permission, obj)
            for subj in direct_subjects:
                subjects.add(subj)
            return

        # Handle union
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                self._expand_permission(rel, obj, namespace, subjects, visited.copy(), depth + 1)
            return

        # Handle tupleToUserset
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find all related objects
                related_objects = self._find_related_objects(obj, tupleset_relation)

                # Expand permission on related objects
                for related_obj in related_objects:
                    related_ns = self.get_namespace(related_obj.entity_type)
                    if related_ns:
                        self._expand_permission(
                            computed_userset,
                            related_obj,
                            related_ns,
                            subjects,
                            visited.copy(),
                            depth + 1,
                        )
            return

        # Direct relation - add all subjects
        direct_subjects = self._get_direct_subjects(permission, obj)
        for subj in direct_subjects:
            subjects.add(subj)

    def _get_direct_subjects(self, relation: str, obj: Entity) -> list[tuple[str, str]]:
        """Get all subjects with direct relation to object.

        Args:
            relation: Relation type
            obj: Object entity

        Returns:
            List of (subject_type, subject_id) tuples
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT subject_type, subject_id
            FROM rebac_tuples
            WHERE relation = ?
              AND object_type = ? AND object_id = ?
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (
                relation,
                obj.entity_type,
                obj.entity_id,
                datetime.utcnow().isoformat(),
            ),
        )

        return [(row["subject_type"], row["subject_id"]) for row in cursor.fetchall()]

    def _get_cached_check(self, subject: Entity, permission: str, obj: Entity) -> bool | None:
        """Get cached permission check result.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity

        Returns:
            Cached result or None if not cached or expired
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT result, expires_at
            FROM rebac_check_cache
            WHERE subject_type = ? AND subject_id = ?
              AND permission = ?
              AND object_type = ? AND object_id = ?
              AND expires_at > ?
            """,
            (
                subject.entity_type,
                subject.entity_id,
                permission,
                obj.entity_type,
                obj.entity_id,
                datetime.utcnow().isoformat(),
            ),
        )

        row = cursor.fetchone()
        if row:
            return bool(row["result"])
        return None

    def _cache_check_result(
        self, subject: Entity, permission: str, obj: Entity, result: bool
    ) -> None:
        """Cache permission check result.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            result: Check result
        """
        cache_id = str(uuid.uuid4())
        computed_at = datetime.utcnow()
        expires_at = computed_at + timedelta(seconds=self.cache_ttl_seconds)

        conn = self._get_connection()
        cursor = conn.cursor()

        # Delete existing cache entry if present
        cursor.execute(
            """
            DELETE FROM rebac_check_cache
            WHERE subject_type = ? AND subject_id = ?
              AND permission = ?
              AND object_type = ? AND object_id = ?
            """,
            (
                subject.entity_type,
                subject.entity_id,
                permission,
                obj.entity_type,
                obj.entity_id,
            ),
        )

        # Insert new cache entry
        cursor.execute(
            """
            INSERT INTO rebac_check_cache (
                cache_id, subject_type, subject_id, permission,
                object_type, object_id, result, computed_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cache_id,
                subject.entity_type,
                subject.entity_id,
                permission,
                obj.entity_type,
                obj.entity_id,
                result,
                computed_at.isoformat(),
                expires_at.isoformat(),
            ),
        )

        conn.commit()

    def _invalidate_cache_for_tuple(self, subject: Entity, _relation: str, obj: Entity) -> None:
        """Invalidate cache entries affected by tuple change.

        When a tuple is added or removed, we need to invalidate cache entries that
        might be affected. For simplicity, we invalidate all cache entries related
        to the subject and object.

        Args:
            subject: Subject entity
            _relation: Relation type (unused, kept for API consistency)
            obj: Object entity
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Invalidate cache entries for this subject
        cursor.execute(
            """
            DELETE FROM rebac_check_cache
            WHERE subject_type = ? AND subject_id = ?
            """,
            (subject.entity_type, subject.entity_id),
        )

        # Invalidate cache entries for this object
        cursor.execute(
            """
            DELETE FROM rebac_check_cache
            WHERE object_type = ? AND object_id = ?
            """,
            (obj.entity_type, obj.entity_id),
        )

        conn.commit()

    def _cleanup_expired_tuples_if_needed(self) -> None:
        """Clean up expired tuples if enough time has passed since last cleanup.

        This method throttles cleanup operations to avoid checking on every rebac_check call.
        Only cleans up if more than 1 second has passed since last cleanup.
        """
        now = datetime.utcnow()

        # Throttle cleanup - only run if more than 1 second since last cleanup
        if self._last_cleanup_time is not None:
            time_since_cleanup = (now - self._last_cleanup_time).total_seconds()
            if time_since_cleanup < 1.0:
                return

        # Update last cleanup time
        self._last_cleanup_time = now

        # Clean up expired tuples (this will also invalidate caches)
        self.cleanup_expired_tuples()

    def cleanup_expired_cache(self) -> int:
        """Remove expired cache entries.

        Returns:
            Number of cache entries removed
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM rebac_check_cache WHERE expires_at <= ?",
            (datetime.utcnow().isoformat(),),
        )

        conn.commit()
        return cursor.rowcount

    def cleanup_expired_tuples(self) -> int:
        """Remove expired relationship tuples.

        Returns:
            Number of tuples removed
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Get expired tuples for changelog
        cursor.execute(
            """
            SELECT tuple_id, subject_type, subject_id, relation, object_type, object_id
            FROM rebac_tuples
            WHERE expires_at IS NOT NULL AND expires_at <= ?
            """,
            (datetime.utcnow().isoformat(),),
        )

        expired_tuples = cursor.fetchall()

        # Delete expired tuples
        cursor.execute(
            """
            DELETE FROM rebac_tuples
            WHERE expires_at IS NOT NULL AND expires_at <= ?
            """,
            (datetime.utcnow().isoformat(),),
        )

        # Log to changelog and invalidate caches for expired tuples
        for row in expired_tuples:
            cursor.execute(
                """
                INSERT INTO rebac_changelog (
                    change_type, tuple_id, subject_type, subject_id,
                    relation, object_type, object_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "DELETE",
                    row["tuple_id"],
                    row["subject_type"],
                    row["subject_id"],
                    row["relation"],
                    row["object_type"],
                    row["object_id"],
                    datetime.utcnow().isoformat(),
                ),
            )

            # Invalidate cache for this tuple
            subject = Entity(row["subject_type"], row["subject_id"])
            obj = Entity(row["object_type"], row["object_id"])
            self._invalidate_cache_for_tuple(subject, row["relation"], obj)

        conn.commit()
        return len(expired_tuples)

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
