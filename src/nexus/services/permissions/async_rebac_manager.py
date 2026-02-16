"""Async ReBAC Manager for relationship-based access control.

This module provides async versions of the ReBAC permission checking operations
using SQLAlchemy async support with asyncpg (PostgreSQL) and aiosqlite (SQLite).

Performance benefits:
- Non-blocking DB operations allow handling more concurrent requests
- 10-50x server throughput improvement under concurrent load
- Integrates seamlessly with FastAPI's async endpoints

Example:
    from nexus.services.permissions.async_rebac_manager import AsyncReBACManager

    # Create async engine
    engine = create_async_engine("postgresql+asyncpg://...")

    # Initialize async manager
    manager = AsyncReBACManager(engine)

    # Use in async context
    result = await manager.rebac_check(
        subject=("user", "alice"),
        permission="read",
        object=("file", "/doc.txt"),
        zone_id="org_123"
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from nexus.core.rebac import (
    WILDCARD_SUBJECT,
    Entity,
    NamespaceConfig,
)
from nexus.services.permissions.cross_zone import CROSS_ZONE_ALLOWED_RELATIONS
from nexus.services.permissions.rebac_cache import ReBACPermissionCache
from nexus.storage.models.permissions import (
    ReBACGroupClosureModel as GC,
)
from nexus.storage.models.permissions import (
    ReBACNamespaceModel as RN,
)
from nexus.storage.models.permissions import (
    ReBACTupleModel as RT,
)

logger = logging.getLogger(__name__)

# Relations that represent group membership
MEMBERSHIP_RELATIONS = frozenset({"member-of", "member", "belongs-to"})


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
        l1_cache_ttl: int = 300,
        enable_metrics: bool = True,
    ):
        """Initialize async ReBAC manager.

        Args:
            engine: SQLAlchemy AsyncEngine (created with create_async_engine)
            cache_ttl_seconds: L2 cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 50 hops)
            enable_l1_cache: Enable in-memory L1 cache (default: True)
            l1_cache_size: L1 cache max entries (default: 10k)
            l1_cache_ttl: L1 cache TTL in seconds (default: 300s)
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

        Uses asyncio.shield() to protect cleanup from task cancellation,
        preventing connection leaks when queries are interrupted by timeouts.

        Usage:
            async with self._session() as session:
                result = await session.execute(...)
        """
        async with self.async_session() as session:
            try:
                yield session
            finally:
                # Shield cleanup from cancellation to prevent connection leaks
                # See: https://medium.com/@har.avetisyan2002/how-we-discovered-and-fixed-a-connection-leak-in-async-sqlalchemy-during-chaos-testing-bf45acf65559
                await asyncio.shield(session.close())

    def _is_postgresql(self) -> bool:
        """Check if using PostgreSQL."""
        return "postgresql" in str(self.engine.url)

    def _now(self) -> datetime | str:
        """Return current UTC time in the format required by the database backend.

        PostgreSQL (asyncpg) requires native datetime objects.
        SQLite (aiosqlite) requires ISO format strings for text comparison.
        """
        now = datetime.now(UTC)
        if self._is_postgresql():
            return now
        return now.isoformat()

    async def _load_namespaces(self) -> None:
        """Load namespace configurations from database."""
        if self._namespaces_loaded:
            return

        async with self._session() as session:
            result = await session.execute(select(RN.namespace_id, RN.object_type, RN.config))
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
        zone_id: str | None = None,  # Issue #773: Defaults to "root" internally
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check permission asynchronously.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., "read", "write")
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-zone isolation
            context: Optional ABAC context for condition evaluation

        Returns:
            True if permission is granted, False otherwise

        Example:
            >>> allowed = await manager.rebac_check(
            ...     subject=("user", "alice"),
            ...     permission="read",
            ...     object=("file", "/doc.txt"),
            ...     zone_id="org_123"
            ... )
        """
        if not zone_id:
            zone_id = "root"

        # Ensure namespaces are loaded
        await self._load_namespaces()

        subject_entity = Entity(subject[0], subject[1])
        obj_entity = Entity(object[0], object[1])

        # Check L1 cache first
        if self._l1_cache:
            cached = self._l1_cache.get(
                subject[0], subject[1], permission, object[0], object[1], zone_id
            )
            if cached is not None:
                return cached

        # Compute permission with delta tracking for XFetch (Issue #718)
        start_time = time.perf_counter()
        result = await self._compute_permission(
            subject_entity, permission, obj_entity, zone_id, context
        )
        delta = time.perf_counter() - start_time
        elapsed_ms = delta * 1000

        # Cache result with XFetch delta
        if self._l1_cache:
            self._l1_cache.set(
                subject[0],
                subject[1],
                permission,
                object[0],
                object[1],
                result,
                zone_id,
                delta=delta,
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
        zone_id: str,
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
                    subject, userset, obj, zone_id, context, visited.copy(), depth + 1
                ):
                    return True
            return False

        # Handle union relations
        if namespace and namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            for rel in union_relations:
                if await self._compute_permission(
                    subject, rel, obj, zone_id, context, visited.copy(), depth + 1
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
                related_objects = await self._find_related_objects(obj, tupleset_relation, zone_id)

                for related_obj in related_objects:
                    if await self._compute_permission(
                        subject,
                        computed_userset,
                        related_obj,
                        zone_id,
                        context,
                        visited.copy(),
                        depth + 1,
                    ):
                        return True
                return False

        # Direct relation check
        return await self._has_direct_relation(subject, permission, obj, zone_id, context)

    async def _has_direct_relation(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check for direct relation tuple in database."""
        async with self._session() as session:
            now_iso = self._now()

            # Check direct concrete tuple
            result = await session.execute(
                select(RT.tuple_id, RT.conditions).where(
                    RT.subject_type == subject.entity_type,
                    RT.subject_id == subject.entity_id,
                    RT.relation == relation,
                    RT.object_type == obj.entity_type,
                    RT.object_id == obj.entity_id,
                    RT.zone_id == zone_id,
                    RT.subject_relation.is_(None),
                    or_(RT.expires_at.is_(None), RT.expires_at >= now_iso),
                )
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

            # Cross-zone check for shared-* relations (PR #647, #648)
            # Cross-zone shares are stored in the resource owner's zone
            # but should be visible when checking from the recipient's zone.
            if relation in CROSS_ZONE_ALLOWED_RELATIONS:
                result = await session.execute(
                    select(RT.tuple_id).where(
                        RT.subject_type == subject.entity_type,
                        RT.subject_id == subject.entity_id,
                        RT.relation == relation,
                        RT.object_type == obj.entity_type,
                        RT.object_id == obj.entity_id,
                        RT.subject_relation.is_(None),
                        or_(RT.expires_at.is_(None), RT.expires_at >= now_iso),
                    )
                )
                if result.fetchone():
                    logger.debug(f"Cross-zone share found: {subject} -> {relation} -> {obj}")
                    return True

            # Check for wildcard/public access (*:*) - Issue #1064
            # Wildcards grant access to ALL subjects regardless of zone.
            # Only check if subject is NOT already the wildcard (avoid infinite loop).
            # Performance: O(1) indexed lookup via idx_rebac_alive_by_subject.
            # Industry standard: SpiceDB, OpenFGA, Ory Keto all use query-time wildcard check.
            if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
                result = await session.execute(
                    select(RT.tuple_id).where(
                        RT.subject_type == WILDCARD_SUBJECT[0],  # "*"
                        RT.subject_id == WILDCARD_SUBJECT[1],  # "*"
                        RT.relation == relation,
                        RT.object_type == obj.entity_type,
                        RT.object_id == obj.entity_id,
                        RT.subject_relation.is_(None),
                        or_(RT.expires_at.is_(None), RT.expires_at >= now_iso),
                    )
                )
                if result.fetchone():
                    logger.debug(f"Wildcard public access: *:* -> {relation} -> {obj}")
                    return True

            # Check userset-as-subject tuples (e.g., group#member)
            result = await session.execute(
                select(RT.subject_type, RT.subject_id, RT.subject_relation).where(
                    RT.relation == relation,
                    RT.object_type == obj.entity_type,
                    RT.object_id == obj.entity_id,
                    RT.subject_relation.isnot(None),
                    RT.zone_id == zone_id,
                    or_(RT.expires_at.is_(None), RT.expires_at >= now_iso),
                )
            )

            for row in result.fetchall():
                userset_type = row[0]
                userset_id = row[1]
                userset_relation = row[2]

                # Recursively check if subject has userset_relation on userset entity
                userset_entity = Entity(userset_type, userset_id)
                if await self._compute_permission(
                    subject, userset_relation, userset_entity, zone_id, context, set(), 0
                ):
                    return True

            return False

    async def _find_related_objects(
        self,
        obj: Entity,
        relation: str,
        zone_id: str,
    ) -> list[Entity]:
        """Find all objects related to obj via relation."""
        # For parent relation, compute from path instead of querying DB
        # This handles cross-zone scenarios where parent tuples are in different zone
        if relation == "parent" and obj.entity_type == "file":
            from pathlib import PurePosixPath

            parent_path = str(PurePosixPath(obj.entity_id).parent)
            if parent_path != obj.entity_id and parent_path != ".":
                return [Entity("file", parent_path)]
            return []

        # For other relations, query the database
        async with self._session() as session:
            result = await session.execute(
                select(RT.object_type, RT.object_id).where(
                    RT.subject_type == obj.entity_type,
                    RT.subject_id == obj.entity_id,
                    RT.relation == relation,
                    RT.zone_id == zone_id,
                    or_(RT.expires_at.is_(None), RT.expires_at >= self._now()),
                )
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
        zone_id: str,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        """Check multiple permissions in batch (async).

        Optimized for bulk operations like list() filtering.
        Fetches all relevant tuples in 1-2 queries, then processes in memory.

        Args:
            checks: List of (subject, permission, object) tuples
            zone_id: Zone ID for all checks

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

        if not zone_id:
            zone_id = "root"

        await self._load_namespaces()

        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool] = {}
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]] = []

        # Check L1 cache first
        if self._l1_cache:
            for check in checks:
                subject, permission, obj = check
                cached = self._l1_cache.get(
                    subject[0], subject[1], permission, obj[0], obj[1], zone_id
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
        tuples_graph = await self._fetch_tuples_bulk(cache_misses, zone_id)

        # Compute permissions using in-memory graph
        memo_cache: dict[tuple[str, str, str, str, str], bool] = {}

        for check in cache_misses:
            subject, permission, obj = check
            subject_entity = Entity(subject[0], subject[1])
            obj_entity = Entity(obj[0], obj[1])

            # Track delta for XFetch (Issue #718)
            start_time = time.perf_counter()
            result = await self._compute_permission_bulk(
                subject_entity, permission, obj_entity, zone_id, tuples_graph, memo_cache
            )
            delta = time.perf_counter() - start_time
            results[check] = result

            # Cache result with XFetch delta
            if self._l1_cache:
                self._l1_cache.set(
                    subject[0],
                    subject[1],
                    permission,
                    obj[0],
                    obj[1],
                    result,
                    zone_id,
                    delta=delta,
                )

        return results

    async def _fetch_tuples_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
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
            now_iso = self._now()

            # Use a simpler approach - fetch tuples matching subjects OR objects
            # (filtering done in Python for simplicity vs complex SQL IN clauses)
            result = await session.execute(
                select(
                    RT.subject_type,
                    RT.subject_id,
                    RT.subject_relation,
                    RT.relation,
                    RT.object_type,
                    RT.object_id,
                    RT.conditions,
                    RT.expires_at,
                ).where(
                    RT.zone_id == zone_id,
                    or_(RT.expires_at.is_(None), RT.expires_at >= now_iso),
                )
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

            # Cross-zone share tuple fetch (PR #647, #648)
            # Fetch shared-* tuples for subjects without zone filter
            result = await session.execute(
                select(
                    RT.subject_type,
                    RT.subject_id,
                    RT.subject_relation,
                    RT.relation,
                    RT.object_type,
                    RT.object_id,
                    RT.conditions,
                    RT.expires_at,
                ).where(
                    RT.relation.in_(["shared-viewer", "shared-editor", "shared-owner"]),
                    or_(RT.expires_at.is_(None), RT.expires_at >= now_iso),
                )
            )
            cross_zone_count = 0
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
                    cross_zone_count += 1
            if cross_zone_count > 0:
                logger.debug(f"Fetched {cross_zone_count} cross-zone share tuples")

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

            # LEOPARD OPTIMIZATION (Issue #840): Add synthetic membership tuples from
            # transitive closure. This allows O(1) group membership lookups instead of
            # O(depth) recursive graph traversal during permission checks.
            try:
                result = await session.execute(
                    select(GC.member_type, GC.member_id, GC.group_type, GC.group_id).where(
                        GC.zone_id == zone_id,
                    )
                )
                leopard_count = 0
                for row in result.fetchall():
                    member = (row[0], row[1])
                    if member in all_subjects:
                        tuples.append(
                            {
                                "subject_type": row[0],
                                "subject_id": row[1],
                                "subject_relation": None,
                                "relation": "member",  # synthetic direct membership
                                "object_type": row[2],
                                "object_id": row[3],
                                "conditions": None,
                                "expires_at": None,
                            }
                        )
                        leopard_count += 1
                if leopard_count > 0:
                    logger.debug(
                        f"[LEOPARD] Added {leopard_count} synthetic membership tuples from closure"
                    )
            except (OperationalError, ProgrammingError) as e:
                # Leopard table may not exist in older databases - graceful fallback
                logger.debug(f"[LEOPARD] Closure lookup failed (table may not exist): {e}")

            logger.debug(f"Fetched {len(tuples)} tuples for bulk check (includes computed parents)")
            return tuples

    async def _compute_permission_bulk(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        zone_id: str,
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
                    zone_id,
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
                    zone_id,
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
                        zone_id,
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
        zone_id: str | None = None,  # Issue #773: Defaults to "root" internally
        subject_relation: str | None = None,
        conditions: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        """Create a relationship tuple (async).

        Args:
            subject: (subject_type, subject_id) tuple
            relation: Relation name (e.g., "owner", "viewer", "parent")
            object: (object_type, object_id) tuple
            zone_id: Zone ID for isolation
            subject_relation: For userset subjects (e.g., "member" in group#member)
            conditions: ABAC conditions for conditional access
            expires_at: Optional expiry time

        Returns:
            tuple_id of created tuple
        """
        import json
        import uuid

        if not zone_id:
            zone_id = "root"

        tuple_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        async with self._session() as session:
            await session.execute(
                insert(RT).values(
                    tuple_id=tuple_id,
                    subject_type=subject[0],
                    subject_id=subject[1],
                    subject_relation=subject_relation,
                    relation=relation,
                    object_type=object[0],
                    object_id=object[1],
                    zone_id=zone_id,
                    conditions=json.dumps(conditions) if conditions else None,
                    expires_at=expires_at,
                    created_at=now,
                )
            )
            await session.commit()

            # LEOPARD (Issue #840): Update closure for membership relations
            if relation in MEMBERSHIP_RELATIONS:
                try:
                    closure_values = {
                        "member_type": subject[0],
                        "member_id": subject[1],
                        "group_type": object[0],
                        "group_id": object[1],
                        "zone_id": zone_id,
                        "depth": 1,
                        "updated_at": func.now(),
                    }
                    closure_index = [
                        GC.member_type,
                        GC.member_id,
                        GC.group_type,
                        GC.group_id,
                        GC.zone_id,
                    ]
                    if self._is_postgresql():
                        from sqlalchemy.dialects.postgresql import insert as pg_insert

                        await session.execute(
                            pg_insert(GC)
                            .values(**closure_values)
                            .on_conflict_do_update(
                                index_elements=closure_index,
                                set_={"depth": 1, "updated_at": func.now()},
                            )
                        )
                    else:
                        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                        await session.execute(
                            sqlite_insert(GC)
                            .values(**closure_values)
                            .on_conflict_do_update(
                                index_elements=closure_index,
                                set_={"depth": 1, "updated_at": func.now()},
                            )
                        )
                    await session.commit()
                    logger.debug(
                        f"[LEOPARD] Added closure: {subject[0]}:{subject[1]} -> "
                        f"{object[0]}:{object[1]}"
                    )
                except (OperationalError, ProgrammingError) as e:
                    # Leopard table may not exist - graceful fallback
                    logger.debug(f"[LEOPARD] Closure update failed: {e}")

        # Invalidate L1 cache for this object
        if self._l1_cache:
            self._l1_cache.invalidate_object(object[0], object[1], zone_id)

        return tuple_id

    async def delete_tuple(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,  # Issue #773: Defaults to "root" internally
    ) -> bool:
        """Delete a relationship tuple (async).

        Args:
            subject: (subject_type, subject_id) tuple
            relation: Relation name
            object: (object_type, object_id) tuple
            zone_id: Zone ID

        Returns:
            True if tuple was deleted, False if not found
        """
        if not zone_id:
            zone_id = "root"

        async with self._session() as session:
            result = await session.execute(
                delete(RT).where(
                    RT.subject_type == subject[0],
                    RT.subject_id == subject[1],
                    RT.relation == relation,
                    RT.object_type == object[0],
                    RT.object_id == object[1],
                    RT.zone_id == zone_id,
                )
            )
            await session.commit()

            deleted: bool = result.rowcount > 0

            # LEOPARD (Issue #840): Update closure for membership relations
            if deleted and relation in MEMBERSHIP_RELATIONS:
                try:
                    # Note: For simplicity, we recompute by deleting and letting
                    # subsequent writes rebuild. Full transitive recompute is complex.
                    await session.execute(
                        delete(GC).where(
                            GC.member_type == subject[0],
                            GC.member_id == subject[1],
                            GC.group_type == object[0],
                            GC.group_id == object[1],
                            GC.zone_id == zone_id,
                        )
                    )
                    await session.commit()
                    logger.debug(
                        f"[LEOPARD] Removed closure: {subject[0]}:{subject[1]} -> "
                        f"{object[0]}:{object[1]}"
                    )
                except (OperationalError, ProgrammingError) as e:
                    logger.debug(f"[LEOPARD] Closure delete failed: {e}")

        # Invalidate L1 cache
        if self._l1_cache:
            self._l1_cache.invalidate_object(object[0], object[1], zone_id)

        return deleted

    def get_l1_cache_stats(self) -> dict[str, Any]:
        """Get L1 cache statistics."""
        if self._l1_cache:
            return self._l1_cache.get_stats()
        return {}


def create_async_engine_from_url(database_url: str) -> AsyncEngine:
    """Create async SQLAlchemy engine from database URL via RecordStoreABC.

    Delegates to SQLAlchemyRecordStore which handles URL conversion
    (postgresql:// -> asyncpg://, sqlite:// -> aiosqlite://) internally.

    Args:
        database_url: Standard database URL

    Returns:
        AsyncEngine instance
    """
    from typing import cast

    from nexus.storage.record_store import SQLAlchemyRecordStore

    store = SQLAlchemyRecordStore(db_url=database_url)
    # Trigger lazy async engine creation and return it
    _ = store.async_session_factory
    return cast("AsyncEngine", store._async_engine)
