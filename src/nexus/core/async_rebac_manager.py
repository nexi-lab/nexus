"""Async ReBAC Manager for relationship-based access control.

This module provides async versions of the ReBAC permission checking operations
using SQLAlchemy async support with asyncpg (PostgreSQL) and aiosqlite (SQLite).

Performance benefits:
- Non-blocking DB operations allow handling more concurrent requests
- 10-50x server throughput improvement under concurrent load
- Integrates seamlessly with FastAPI's async endpoints

Example:
    from nexus.core.async_rebac_manager import AsyncReBACManager

    # Create async engine
    engine = create_async_engine("postgresql+asyncpg://...")

    # Initialize async manager
    manager = AsyncReBACManager(engine)

    # Use in async context
    result = await manager.rebac_check(
        subject=("user", "alice"),
        permission="read",
        object=("file", "/doc.txt"),
        tenant_id="org_123"
    )
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from nexus.core.rebac import CROSS_TENANT_ALLOWED_RELATIONS, Entity, NamespaceConfig
from nexus.core.rebac_cache import ReBACPermissionCache

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class AsyncReBACManager:
    """Async manager for ReBAC operations.

    Provides non-blocking permission checking using async database drivers.
    Compatible with FastAPI, asyncio, and other async frameworks.

    Key methods (all async):
    - rebac_check(): Check single permission
    - rebac_check_bulk(): Check multiple permissions efficiently
    - write_tuple(): Create relationship tuple
    - delete_tuple(): Remove relationship tuple
    """

    def __init__(
        self,
        engine: AsyncEngine,
        cache_ttl_seconds: int = 300,
        max_depth: int = 50,
        enable_l1_cache: bool = True,
        l1_cache_size: int = 10000,
        l1_cache_ttl: int = 60,
        enable_metrics: bool = True,
    ):
        """Initialize async ReBAC manager.

        Args:
            engine: SQLAlchemy AsyncEngine (created with create_async_engine)
            cache_ttl_seconds: L2 cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 50 hops)
            enable_l1_cache: Enable in-memory L1 cache (default: True)
            l1_cache_size: L1 cache max entries (default: 10k)
            l1_cache_ttl: L1 cache TTL in seconds (default: 60s)
            enable_metrics: Track cache metrics (default: True)
        """
        self.engine = engine
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_depth = max_depth

        # Create async session factory
        self.async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Initialize L1 in-memory cache (thread-safe, used from async context)
        self._l1_cache: ReBACPermissionCache | None = None
        if enable_l1_cache:
            self._l1_cache = ReBACPermissionCache(
                max_size=l1_cache_size,
                ttl_seconds=l1_cache_ttl,
                enable_metrics=enable_metrics,
            )
            logger.info(f"Async L1 cache enabled: max_size={l1_cache_size}, ttl={l1_cache_ttl}s")

        # Namespace cache (loaded on first use)
        self._namespaces: dict[str, NamespaceConfig] = {}
        self._namespaces_loaded = False

    @asynccontextmanager
    async def _session(self) -> Any:
        """Get async database session.

        Usage:
            async with self._session() as session:
                result = await session.execute(...)
        """
        async with self.async_session() as session:
            try:
                yield session
            finally:
                await session.close()

    def _is_postgresql(self) -> bool:
        """Check if using PostgreSQL."""
        return "postgresql" in str(self.engine.url)

    async def _load_namespaces(self) -> None:
        """Load namespace configurations from database."""
        if self._namespaces_loaded:
            return

        async with self._session() as session:
            result = await session.execute(
                text("SELECT namespace_id, object_type, config FROM rebac_namespaces")
            )
            rows = result.fetchall()

            for row in rows:
                import json

                config = json.loads(row[2]) if isinstance(row[2], str) else row[2]
                self._namespaces[row[1]] = NamespaceConfig(
                    namespace_id=row[0],
                    object_type=row[1],
                    config=config,
                )

            self._namespaces_loaded = True
            logger.debug(f"Loaded {len(self._namespaces)} namespace configs")

    def get_namespace(self, object_type: str) -> NamespaceConfig | None:
        """Get namespace configuration for object type."""
        return self._namespaces.get(object_type)

    async def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        tenant_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check permission asynchronously.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., "read", "write")
            object: (object_type, object_id) tuple
            tenant_id: Tenant ID for multi-tenant isolation
            context: Optional ABAC context for condition evaluation

        Returns:
            True if permission is granted, False otherwise

        Example:
            >>> allowed = await manager.rebac_check(
            ...     subject=("user", "alice"),
            ...     permission="read",
            ...     object=("file", "/doc.txt"),
            ...     tenant_id="org_123"
            ... )
        """
        if not tenant_id:
            tenant_id = "default"

        # Ensure namespaces are loaded
        await self._load_namespaces()

        subject_entity = Entity(subject[0], subject[1])
        obj_entity = Entity(object[0], object[1])

        # Check L1 cache first
        if self._l1_cache:
            cached = self._l1_cache.get(
                subject[0], subject[1], permission, object[0], object[1], tenant_id
            )
            if cached is not None:
                return cached

        # Compute permission
        start_time = time.perf_counter()
        result = await self._compute_permission(
            subject_entity, permission, obj_entity, tenant_id, context
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Cache result
        if self._l1_cache:
            self._l1_cache.set(
                subject[0], subject[1], permission, object[0], object[1], result, tenant_id
            )

        logger.debug(
            f"[ASYNC-REBAC] {subject[0]}:{subject[1]} {permission} {object[0]}:{object[1]} = {result} ({elapsed_ms:.1f}ms)"
        )

        return result

    async def _compute_permission(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tenant_id: str,
        context: dict[str, Any] | None = None,
        visited: set[tuple[str, str, str, str, str]] | None = None,
        depth: int = 0,
    ) -> bool:
        """Compute permission with async graph traversal.

        Handles:
        - Direct relations
        - Permission expansion via namespace config
        - Union relations
        - TupleToUserset (parent/group inheritance)
        """
        if visited is None:
            visited = set()

        # Depth limit
        if depth > self.max_depth:
            logger.warning(f"Max depth {self.max_depth} exceeded, denying")
            return False

        # Cycle detection
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

        # Check if permission is mapped to relations
        if namespace and namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            for userset in usersets:
                if await self._compute_permission(
                    subject, userset, obj, tenant_id, context, visited.copy(), depth + 1
                ):
                    return True
            return False

        # Handle union relations
        if namespace and namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                if await self._compute_permission(
                    subject, rel, obj, tenant_id, context, visited.copy(), depth + 1
                ):
                    return True
            return False

        # Handle tupleToUserset (parent/group inheritance)
        if namespace and namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find related objects
                related_objects = await self._find_related_objects(
                    obj, tupleset_relation, tenant_id
                )

                for related_obj in related_objects:
                    if await self._compute_permission(
                        subject,
                        computed_userset,
                        related_obj,
                        tenant_id,
                        context,
                        visited.copy(),
                        depth + 1,
                    ):
                        return True
                return False

        # Direct relation check
        return await self._has_direct_relation(subject, permission, obj, tenant_id, context)

    async def _has_direct_relation(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        tenant_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check for direct relation tuple in database."""
        async with self._session() as session:
            # Check direct concrete tuple
            query = text("""
                SELECT tuple_id, conditions FROM rebac_tuples
                WHERE subject_type = :subject_type AND subject_id = :subject_id
                  AND relation = :relation
                  AND object_type = :object_type AND object_id = :object_id
                  AND tenant_id = :tenant_id
                  AND subject_relation IS NULL
                  AND (expires_at IS NULL OR expires_at >= :now)
            """)

            result = await session.execute(
                query,
                {
                    "subject_type": subject.entity_type,
                    "subject_id": subject.entity_id,
                    "relation": relation,
                    "object_type": obj.entity_type,
                    "object_id": obj.entity_id,
                    "tenant_id": tenant_id,
                    "now": datetime.now(UTC).isoformat(),
                },
            )
            row = result.fetchone()

            if row:
                # Check conditions if present
                conditions_json = row[1]
                if conditions_json:
                    import json

                    conditions = (
                        json.loads(conditions_json)
                        if isinstance(conditions_json, str)
                        else conditions_json
                    )
                    if not self._evaluate_conditions(conditions, context):
                        pass  # Continue to check userset
                    else:
                        return True
                else:
                    return True

            # Cross-tenant check for shared-* relations (PR #647, #648)
            # Cross-tenant shares are stored in the resource owner's tenant
            # but should be visible when checking from the recipient's tenant.
            if relation in CROSS_TENANT_ALLOWED_RELATIONS:
                cross_tenant_query = text("""
                    SELECT tuple_id FROM rebac_tuples
                    WHERE subject_type = :subject_type AND subject_id = :subject_id
                      AND relation = :relation
                      AND object_type = :object_type AND object_id = :object_id
                      AND subject_relation IS NULL
                      AND (expires_at IS NULL OR expires_at >= :now)
                """)
                result = await session.execute(
                    cross_tenant_query,
                    {
                        "subject_type": subject.entity_type,
                        "subject_id": subject.entity_id,
                        "relation": relation,
                        "object_type": obj.entity_type,
                        "object_id": obj.entity_id,
                        "now": datetime.now(UTC).isoformat(),
                    },
                )
                if result.fetchone():
                    logger.debug(f"Cross-tenant share found: {subject} -> {relation} -> {obj}")
                    return True

            # Check userset-as-subject tuples (e.g., group#member)
            query = text("""
                SELECT subject_type, subject_id, subject_relation
                FROM rebac_tuples
                WHERE relation = :relation
                  AND object_type = :object_type AND object_id = :object_id
                  AND subject_relation IS NOT NULL
                  AND tenant_id = :tenant_id
                  AND (expires_at IS NULL OR expires_at >= :now)
            """)

            result = await session.execute(
                query,
                {
                    "relation": relation,
                    "object_type": obj.entity_type,
                    "object_id": obj.entity_id,
                    "tenant_id": tenant_id,
                    "now": datetime.now(UTC).isoformat(),
                },
            )

            for row in result.fetchall():
                userset_type = row[0]
                userset_id = row[1]
                userset_relation = row[2]

                # Recursively check if subject has userset_relation on userset entity
                userset_entity = Entity(userset_type, userset_id)
                if await self._compute_permission(
                    subject, userset_relation, userset_entity, tenant_id, context, set(), 0
                ):
                    return True

            return False

    async def _find_related_objects(
        self,
        obj: Entity,
        relation: str,
        tenant_id: str,
    ) -> list[Entity]:
        """Find all objects related to obj via relation."""
        async with self._session() as session:
            # For parent relation, compute from path instead of querying DB
            # This handles cross-tenant scenarios where parent tuples are in different tenant
            if relation == "parent" and obj.entity_type == "file":
                from pathlib import PurePosixPath

                parent_path = str(PurePosixPath(obj.entity_id).parent)
                if parent_path != obj.entity_id and parent_path != ".":
                    return [Entity("file", parent_path)]
                return []

            # For other relations, query the database
            query = text("""
                SELECT object_type, object_id
                FROM rebac_tuples
                WHERE subject_type = :subject_type AND subject_id = :subject_id
                  AND relation = :relation
                  AND tenant_id = :tenant_id
                  AND (expires_at IS NULL OR expires_at >= :now)
            """)

            result = await session.execute(
                query,
                {
                    "subject_type": obj.entity_type,
                    "subject_id": obj.entity_id,
                    "relation": relation,
                    "tenant_id": tenant_id,
                    "now": datetime.now(UTC).isoformat(),
                },
            )

            return [Entity(row[0], row[1]) for row in result.fetchall()]

    def _evaluate_conditions(
        self, conditions: dict[str, Any], context: dict[str, Any] | None
    ) -> bool:
        """Evaluate ABAC conditions against context."""
        if not conditions:
            return True
        if not context:
            return False

        # Simple condition evaluation (key: expected_value)
        for key, expected in conditions.items():
            if key not in context:
                return False
            if context[key] != expected:
                return False

        return True

    async def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        tenant_id: str,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        """Check multiple permissions in batch (async).

        Optimized for bulk operations like list() filtering.
        Fetches all relevant tuples in 1-2 queries, then processes in memory.

        Args:
            checks: List of (subject, permission, object) tuples
            tenant_id: Tenant ID for all checks

        Returns:
            Dict mapping each check to its result (True/False)

        Example:
            >>> checks = [
            ...     (("user", "alice"), "read", ("file", "/a.txt")),
            ...     (("user", "alice"), "read", ("file", "/b.txt")),
            ... ]
            >>> results = await manager.rebac_check_bulk(checks, "org_123")
        """
        if not checks:
            return {}

        if not tenant_id:
            tenant_id = "default"

        await self._load_namespaces()

        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool] = {}
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]] = []

        # Check L1 cache first
        if self._l1_cache:
            for check in checks:
                subject, permission, obj = check
                cached = self._l1_cache.get(
                    subject[0], subject[1], permission, obj[0], obj[1], tenant_id
                )
                if cached is not None:
                    results[check] = cached
                else:
                    cache_misses.append(check)
        else:
            cache_misses = list(checks)

        if not cache_misses:
            return results

        # Fetch all relevant tuples in bulk
        tuples_graph = await self._fetch_tuples_bulk(cache_misses, tenant_id)

        # Compute permissions using in-memory graph
        memo_cache: dict[tuple[str, str, str, str, str], bool] = {}

        for check in cache_misses:
            subject, permission, obj = check
            subject_entity = Entity(subject[0], subject[1])
            obj_entity = Entity(obj[0], obj[1])

            result = await self._compute_permission_bulk(
                subject_entity, permission, obj_entity, tenant_id, tuples_graph, memo_cache
            )
            results[check] = result

            # Cache result
            if self._l1_cache:
                self._l1_cache.set(
                    subject[0], subject[1], permission, obj[0], obj[1], result, tenant_id
                )

        return results

    async def _fetch_tuples_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch all relevant tuples for bulk permission checks."""
        # Collect all subjects and objects
        all_subjects: set[tuple[str, str]] = set()
        all_objects: set[tuple[str, str]] = set()

        for subject, _, obj in checks:
            all_subjects.add(subject)
            all_objects.add(obj)

        # For file paths, also include ancestors
        for obj_type, obj_id in list(all_objects):
            if obj_type == "file" and "/" in obj_id:
                parts = obj_id.strip("/").split("/")
                for i in range(len(parts), 0, -1):
                    ancestor = "/" + "/".join(parts[:i])
                    all_objects.add(("file", ancestor))
                    all_subjects.add(("file", ancestor))
                all_objects.add(("file", "/"))

        async with self._session() as session:
            # Use a simpler approach - fetch tuples matching subjects OR objects
            # (filtering done in Python for simplicity vs complex SQL IN clauses)
            query = text("""
                SELECT subject_type, subject_id, subject_relation, relation,
                       object_type, object_id, conditions, expires_at
                FROM rebac_tuples
                WHERE tenant_id = :tenant_id
                  AND (expires_at IS NULL OR expires_at >= :now)
            """)

            result = await session.execute(
                query,
                {
                    "tenant_id": tenant_id,
                    "now": datetime.now(UTC).isoformat(),
                },
            )

            tuples = []
            for row in result.fetchall():
                # Filter in Python (simpler than complex SQL IN clauses)
                subj = (row[0], row[1])
                obj = (row[4], row[5])
                if subj in all_subjects or obj in all_objects:
                    tuples.append(
                        {
                            "subject_type": row[0],
                            "subject_id": row[1],
                            "subject_relation": row[2],
                            "relation": row[3],
                            "object_type": row[4],
                            "object_id": row[5],
                            "conditions": row[6],
                            "expires_at": row[7],
                        }
                    )

            # Cross-tenant share tuple fetch (PR #647, #648)
            # Fetch shared-* tuples for subjects without tenant filter
            cross_tenant_query = text("""
                SELECT subject_type, subject_id, subject_relation, relation,
                       object_type, object_id, conditions, expires_at
                FROM rebac_tuples
                WHERE relation IN ('shared-viewer', 'shared-editor', 'shared-owner')
                  AND (expires_at IS NULL OR expires_at >= :now)
            """)
            result = await session.execute(
                cross_tenant_query,
                {"now": datetime.now(UTC).isoformat()},
            )
            cross_tenant_count = 0
            for row in result.fetchall():
                subj = (row[0], row[1])
                obj = (row[4], row[5])
                if subj in all_subjects or obj in all_objects:
                    tuples.append(
                        {
                            "subject_type": row[0],
                            "subject_id": row[1],
                            "subject_relation": row[2],
                            "relation": row[3],
                            "object_type": row[4],
                            "object_id": row[5],
                            "conditions": row[6],
                            "expires_at": row[7],
                        }
                    )
                    cross_tenant_count += 1
            if cross_tenant_count > 0:
                logger.debug(f"Fetched {cross_tenant_count} cross-tenant share tuples")

            # Compute parent tuples in memory (PR #648)
            # For file paths, parent relationships are deterministic from path
            from pathlib import PurePosixPath

            for obj_type, obj_id in all_objects:
                if obj_type == "file":
                    parent_path = str(PurePosixPath(obj_id).parent)
                    if parent_path != obj_id and parent_path != ".":
                        tuples.append(
                            {
                                "subject_type": "file",
                                "subject_id": obj_id,
                                "subject_relation": None,
                                "relation": "parent",
                                "object_type": "file",
                                "object_id": parent_path,
                                "conditions": None,
                                "expires_at": None,
                            }
                        )

            logger.debug(f"Fetched {len(tuples)} tuples for bulk check (includes computed parents)")
            return tuples

    async def _compute_permission_bulk(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tenant_id: str,
        tuples_graph: list[dict[str, Any]],
        memo_cache: dict[tuple[str, str, str, str, str], bool],
        depth: int = 0,
        visited: set[tuple[str, str, str, str, str]] | None = None,
    ) -> bool:
        """Compute permission using pre-fetched tuples graph."""
        if visited is None:
            visited = set()

        # Check memo cache
        memo_key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        if memo_key in memo_cache:
            return memo_cache[memo_key]

        # Depth limit
        if depth > self.max_depth:
            return False

        # Cycle detection
        if memo_key in visited:
            return False
        visited.add(memo_key)

        # Get namespace config
        namespace = self.get_namespace(obj.entity_type)

        # Check permission mapping
        if namespace and namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            result = False
            for userset in usersets:
                if await self._compute_permission_bulk(
                    subject,
                    userset,
                    obj,
                    tenant_id,
                    tuples_graph,
                    memo_cache,
                    depth + 1,
                    visited.copy(),
                ):
                    result = True
                    break
            memo_cache[memo_key] = result
            return result

        # Handle union
        if namespace and namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            result = False
            for rel in union_relations:
                if await self._compute_permission_bulk(
                    subject,
                    rel,
                    obj,
                    tenant_id,
                    tuples_graph,
                    memo_cache,
                    depth + 1,
                    visited.copy(),
                ):
                    result = True
                    break
            memo_cache[memo_key] = result
            return result

        # Handle tupleToUserset
        if namespace and namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find related objects in graph
                related = self._find_related_in_graph(obj, tupleset_relation, tuples_graph)
                result = False
                for related_obj in related:
                    if await self._compute_permission_bulk(
                        subject,
                        computed_userset,
                        related_obj,
                        tenant_id,
                        tuples_graph,
                        memo_cache,
                        depth + 1,
                        visited.copy(),
                    ):
                        result = True
                        break
                memo_cache[memo_key] = result
                return result

        # Direct relation check in graph
        result = self._check_direct_in_graph(subject, permission, obj, tuples_graph)
        memo_cache[memo_key] = result
        return result

    def _check_direct_in_graph(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        tuples_graph: list[dict[str, Any]],
    ) -> bool:
        """Check for direct relation in pre-fetched tuples."""
        for t in tuples_graph:
            if (
                t["subject_type"] == subject.entity_type
                and t["subject_id"] == subject.entity_id
                and t["relation"] == relation
                and t["object_type"] == obj.entity_type
                and t["object_id"] == obj.entity_id
                and t["subject_relation"] is None
            ):
                return True
        return False

    def _find_related_in_graph(
        self,
        obj: Entity,
        relation: str,
        tuples_graph: list[dict[str, Any]],
    ) -> list[Entity]:
        """Find related objects in pre-fetched tuples."""
        related = []
        for t in tuples_graph:
            if (
                t["subject_type"] == obj.entity_type
                and t["subject_id"] == obj.entity_id
                and t["relation"] == relation
            ):
                related.append(Entity(t["object_type"], t["object_id"]))
        return related

    async def write_tuple(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        tenant_id: str | None = None,
        subject_relation: str | None = None,
        conditions: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        """Create a relationship tuple (async).

        Args:
            subject: (subject_type, subject_id) tuple
            relation: Relation name (e.g., "owner", "viewer", "parent")
            object: (object_type, object_id) tuple
            tenant_id: Tenant ID for isolation
            subject_relation: For userset subjects (e.g., "member" in group#member)
            conditions: ABAC conditions for conditional access
            expires_at: Optional expiry time

        Returns:
            tuple_id of created tuple
        """
        import json
        import uuid

        if not tenant_id:
            tenant_id = "default"

        tuple_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        async with self._session() as session:
            await session.execute(
                text("""
                    INSERT INTO rebac_tuples (
                        tuple_id, subject_type, subject_id, subject_relation,
                        relation, object_type, object_id, tenant_id,
                        conditions, expires_at, created_at, updated_at
                    ) VALUES (
                        :tuple_id, :subject_type, :subject_id, :subject_relation,
                        :relation, :object_type, :object_id, :tenant_id,
                        :conditions, :expires_at, :created_at, :updated_at
                    )
                """),
                {
                    "tuple_id": tuple_id,
                    "subject_type": subject[0],
                    "subject_id": subject[1],
                    "subject_relation": subject_relation,
                    "relation": relation,
                    "object_type": object[0],
                    "object_id": object[1],
                    "tenant_id": tenant_id,
                    "conditions": json.dumps(conditions) if conditions else None,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await session.commit()

        # Invalidate L1 cache for this object
        if self._l1_cache:
            self._l1_cache.invalidate_object(object[0], object[1], tenant_id)

        return tuple_id

    async def delete_tuple(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        tenant_id: str | None = None,
    ) -> bool:
        """Delete a relationship tuple (async).

        Args:
            subject: (subject_type, subject_id) tuple
            relation: Relation name
            object: (object_type, object_id) tuple
            tenant_id: Tenant ID

        Returns:
            True if tuple was deleted, False if not found
        """
        if not tenant_id:
            tenant_id = "default"

        async with self._session() as session:
            result = await session.execute(
                text("""
                    DELETE FROM rebac_tuples
                    WHERE subject_type = :subject_type AND subject_id = :subject_id
                      AND relation = :relation
                      AND object_type = :object_type AND object_id = :object_id
                      AND tenant_id = :tenant_id
                """),
                {
                    "subject_type": subject[0],
                    "subject_id": subject[1],
                    "relation": relation,
                    "object_type": object[0],
                    "object_id": object[1],
                    "tenant_id": tenant_id,
                },
            )
            await session.commit()

            deleted: bool = result.rowcount > 0

        # Invalidate L1 cache
        if self._l1_cache:
            self._l1_cache.invalidate_object(object[0], object[1], tenant_id)

        return deleted

    def get_l1_cache_stats(self) -> dict[str, Any]:
        """Get L1 cache statistics."""
        if self._l1_cache:
            return self._l1_cache.get_stats()
        return {}


def create_async_engine_from_url(database_url: str) -> AsyncEngine:
    """Create async SQLAlchemy engine from database URL.

    Automatically selects the correct async driver:
    - postgresql:// -> postgresql+asyncpg://
    - sqlite:// -> sqlite+aiosqlite://

    Args:
        database_url: Standard database URL

    Returns:
        AsyncEngine instance
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    # Convert to async driver URL
    if database_url.startswith("postgresql://"):
        async_url = database_url.replace("postgresql://", "postgresql+asyncpg://")
    elif database_url.startswith("sqlite://"):
        async_url = database_url.replace("sqlite://", "sqlite+aiosqlite://")
    else:
        # Already has async driver specified
        async_url = database_url

    return create_async_engine(async_url, echo=False)
