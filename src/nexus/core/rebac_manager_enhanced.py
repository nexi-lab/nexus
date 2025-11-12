"""
Enhanced ReBAC Manager with P0 Fixes

This module implements critical security and reliability fixes for GA:
- P0-1: Consistency levels and version tokens
- P0-2: Tenant scoping (integrates TenantAwareReBACManager)
- P0-5: Graph limits and DoS protection

Usage:
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager, ConsistencyLevel

    manager = EnhancedReBACManager(engine)

    # P0-1: Explicit consistency control
    result = manager.rebac_check(
        subject=("user", "alice"),
        permission="read",
        object=("file", "/doc.txt"),
        tenant_id="org_123",
        consistency=ConsistencyLevel.STRONG,  # Bypass cache
    )

    # P0-5: Graph limits prevent DoS
    # Automatically enforces timeout, fan-out, and memory limits
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from nexus.core.rebac import Entity
from nexus.core.rebac_manager_tenant_aware import TenantAwareReBACManager

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


# ============================================================================
# P0-1: Consistency Levels and Version Tokens
# ============================================================================


class ConsistencyLevel(Enum):
    """Consistency levels for permission checks.

    Controls cache behavior and staleness guarantees:
    - EVENTUAL: Use cache (up to 5min staleness), fastest
    - BOUNDED: Max 1s staleness
    - STRONG: Bypass cache, fresh read, slowest but most accurate
    """

    EVENTUAL = "eventual"  # Use cache (5min staleness)
    BOUNDED = "bounded"  # Max 1s staleness
    STRONG = "strong"  # Bypass cache, fresh read


@dataclass
class CheckResult:
    """Result of a permission check with consistency metadata.

    Attributes:
        allowed: Whether permission is granted
        consistency_token: Version token for this check (monotonic counter)
        decision_time_ms: Time taken to compute decision
        cached: Whether result came from cache
        cache_age_ms: Age of cached result (None if not cached)
        traversal_stats: Graph traversal statistics
        indeterminate: Whether decision was indeterminate (denied due to limits, not policy)
        limit_exceeded: The limit that was exceeded (if indeterminate=True)
    """

    allowed: bool
    consistency_token: str
    decision_time_ms: float
    cached: bool
    cache_age_ms: float | None = None
    traversal_stats: TraversalStats | None = None
    indeterminate: bool = False  # BUGFIX (Issue #5): Track limit-driven denials
    limit_exceeded: GraphLimitExceeded | None = None  # BUGFIX (Issue #5): Which limit was hit


@dataclass
class TraversalStats:
    """Statistics from graph traversal (P0-5).

    Used for monitoring and debugging graph limits.
    """

    queries: int = 0
    nodes_visited: int = 0
    max_depth_reached: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    duration_ms: float = 0.0


# ============================================================================
# P0-5: Graph Limits and DoS Protection
# ============================================================================


class GraphLimits:
    """Hard limits for graph traversal to prevent DoS attacks.

    These limits ensure permission checks complete within bounded time
    and memory, even with pathological graphs.
    """

    MAX_DEPTH = 50  # Max recursion depth (increased for deep directory hierarchies)
    MAX_FAN_OUT = 1000  # Max edges per union/expand
    MAX_EXECUTION_TIME_MS = 1000  # Hard timeout (1000ms = 1 second, increased for deep hierarchies with parent traversal)
    MAX_VISITED_NODES = 10000  # Memory bound
    MAX_TUPLE_QUERIES = 100  # DB query limit


class GraphLimitExceeded(Exception):
    """Raised when graph traversal exceeds limits.

    Attributes:
        limit_type: Type of limit exceeded (depth, fan_out, timeout, nodes, queries)
        limit_value: Configured limit value
        actual_value: Actual value when limit was hit
        path: Partial proof path before limit
    """

    def __init__(
        self,
        limit_type: str,
        limit_value: int | float,
        actual_value: int | float,
        path: list[str] | None = None,
    ):
        self.limit_type = limit_type
        self.limit_value = limit_value
        self.actual_value = actual_value
        self.path = path or []
        super().__init__(f"Graph {limit_type} limit exceeded: {actual_value} > {limit_value}")

    def to_http_error(self) -> dict[str, Any]:
        """Convert to HTTP error response."""
        if self.limit_type == "timeout":
            return {
                "code": 503,
                "message": "Permission check timeout",
                "limit": self.limit_value,
                "actual": self.actual_value,
            }
        else:
            return {
                "code": 429,
                "message": f"Graph {self.limit_type} limit exceeded",
                "limit": self.limit_value,
                "actual": self.actual_value,
            }


# ============================================================================
# Enhanced ReBAC Manager (All P0 Fixes Integrated)
# ============================================================================


class EnhancedReBACManager(TenantAwareReBACManager):
    """ReBAC Manager with all P0 fixes integrated.

    Combines:
    - P0-1: Consistency levels and version tokens
    - P0-2: Tenant scoping (via TenantAwareReBACManager)
    - P0-5: Graph limits and DoS protection

    This is the GA-ready ReBAC implementation.
    """

    def __init__(
        self,
        engine: Engine,
        cache_ttl_seconds: int = 300,
        max_depth: int = 50,
        enforce_tenant_isolation: bool = True,
        enable_graph_limits: bool = True,
    ):
        """Initialize enhanced ReBAC manager.

        Args:
            engine: SQLAlchemy database engine
            cache_ttl_seconds: Cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
            enforce_tenant_isolation: Enable tenant isolation checks (default: True)
            enable_graph_limits: Enable graph limit enforcement (default: True)
        """
        super().__init__(engine, cache_ttl_seconds, max_depth, enforce_tenant_isolation)
        self.enable_graph_limits = enable_graph_limits
        # REMOVED: self._version_counter (replaced with DB sequence in Issue #2 fix)

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        consistency: ConsistencyLevel = ConsistencyLevel.EVENTUAL,
    ) -> bool:
        """Check permission with explicit consistency level (P0-1).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation
            tenant_id: Tenant ID to scope check
            consistency: Consistency level (EVENTUAL, BOUNDED, STRONG)

        Returns:
            True if permission is granted, False otherwise

        Raises:
            GraphLimitExceeded: If graph traversal exceeds limits (P0-5)
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.info(
            f"EnhancedReBACManager.rebac_check called: enforce_tenant_isolation={self.enforce_tenant_isolation}, MAX_DEPTH={GraphLimits.MAX_DEPTH}"
        )

        # If tenant isolation is disabled, use base ReBACManager implementation
        if not self.enforce_tenant_isolation:
            from nexus.core.rebac_manager import ReBACManager

            logger.info(f"  -> Falling back to base ReBACManager, base max_depth={self.max_depth}")
            return ReBACManager.rebac_check(self, subject, permission, object, context, tenant_id)

        logger.info("  -> Using rebac_check_detailed")
        result = self.rebac_check_detailed(
            subject, permission, object, context, tenant_id, consistency
        )
        logger.info(
            f"  -> rebac_check_detailed result: allowed={result.allowed}, indeterminate={result.indeterminate}"
        )
        return result.allowed

    def rebac_check_detailed(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        consistency: ConsistencyLevel = ConsistencyLevel.EVENTUAL,
    ) -> CheckResult:
        """Check permission with detailed result metadata (P0-1).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation
            tenant_id: Tenant ID to scope check
            consistency: Consistency level

        Returns:
            CheckResult with consistency metadata and traversal stats
        """
        # BUGFIX (Issue #3): Fail fast on missing tenant_id in production
        # In production, missing tenant_id is a security issue - reject immediately
        if not tenant_id:
            import logging
            import os

            logger = logging.getLogger(__name__)

            # Check if we're in production mode (via env var or config)
            is_production = (
                os.getenv("NEXUS_ENV") == "production" or os.getenv("ENVIRONMENT") == "production"
            )

            if is_production:
                # SECURITY: In production, missing tenant_id is a critical error
                logger.error("rebac_check called without tenant_id in production - REJECTING")
                raise ValueError(
                    "tenant_id is required for permission checks in production. "
                    "Missing tenant_id can lead to cross-tenant data leaks. "
                    "Set NEXUS_ENV=development to allow defaulting for local testing."
                )
            else:
                # Development/test: Allow defaulting but log stack trace for debugging
                import traceback

                logger.warning(
                    f"rebac_check called without tenant_id, defaulting to 'default'. "
                    f"This is only allowed in development. Stack:\n{''.join(traceback.format_stack()[-5:])}"
                )
                tenant_id = "default"

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        # BUGFIX (Issue #4): Use perf_counter for elapsed time measurement
        # time.time() uses wall clock which can jump (NTP, DST), causing incorrect timeouts
        # perf_counter() is monotonic and immune to clock adjustments
        start_time = time.perf_counter()

        # Clean up expired tuples
        self._cleanup_expired_tuples_if_needed()

        # P0-1: Handle consistency levels
        if consistency == ConsistencyLevel.STRONG:
            # Strong consistency: Bypass cache, fresh read
            stats = TraversalStats()
            limit_error = None  # Track if we hit a limit
            try:
                result = self._compute_permission_with_limits(
                    subject_entity, permission, object_entity, tenant_id, stats, context
                )
            except GraphLimitExceeded as e:
                # BUGFIX (Issue #5): Fail-closed on limit exceeded, but mark as indeterminate
                import logging

                logger = logging.getLogger(__name__)
                logger.error(
                    f"GraphLimitExceeded caught: limit_type={e.limit_type}, limit_value={e.limit_value}, actual_value={e.actual_value}"
                )
                result = False
                limit_error = e

            decision_time_ms = (time.perf_counter() - start_time) * 1000
            stats.duration_ms = decision_time_ms

            return CheckResult(
                allowed=result,
                consistency_token=self._get_version_token(tenant_id),
                decision_time_ms=decision_time_ms,
                cached=False,
                cache_age_ms=None,
                traversal_stats=stats,
                indeterminate=limit_error is not None,
                limit_exceeded=limit_error,
            )

        elif consistency == ConsistencyLevel.BOUNDED:
            # Bounded consistency: Max 1s staleness
            cached = self._get_cached_check_tenant_aware_bounded(
                subject_entity, permission, object_entity, tenant_id, max_age_seconds=1
            )
            if cached is not None:
                decision_time_ms = (time.perf_counter() - start_time) * 1000
                return CheckResult(
                    allowed=cached,
                    consistency_token=self._get_version_token(tenant_id),
                    decision_time_ms=decision_time_ms,
                    cached=True,
                    cache_age_ms=None,  # Within 1s bound
                    traversal_stats=None,
                )

            # Cache miss or too old - compute fresh
            stats = TraversalStats()
            limit_error = None
            try:
                result = self._compute_permission_with_limits(
                    subject_entity, permission, object_entity, tenant_id, stats, context
                )
            except GraphLimitExceeded as e:
                result = False
                limit_error = e

            self._cache_check_result_tenant_aware(
                subject_entity, permission, object_entity, tenant_id, result
            )

            decision_time_ms = (time.perf_counter() - start_time) * 1000
            stats.duration_ms = decision_time_ms

            return CheckResult(
                allowed=result,
                consistency_token=self._get_version_token(tenant_id),
                decision_time_ms=decision_time_ms,
                cached=False,
                cache_age_ms=None,
                traversal_stats=stats,
                indeterminate=limit_error is not None,
                limit_exceeded=limit_error,
            )

        else:  # ConsistencyLevel.EVENTUAL (default)
            # Eventual consistency: Use cache (up to cache_ttl_seconds staleness)
            import logging

            logger = logging.getLogger(__name__)
            cached = self._get_cached_check_tenant_aware(
                subject_entity, permission, object_entity, tenant_id
            )
            if cached is not None:
                logger.info(f"  -> CACHE HIT: returning cached result={cached}")
                decision_time_ms = (time.perf_counter() - start_time) * 1000
                return CheckResult(
                    allowed=cached,
                    consistency_token=self._get_version_token(tenant_id),
                    decision_time_ms=decision_time_ms,
                    cached=True,
                    cache_age_ms=None,  # Could be up to cache_ttl_seconds old
                    traversal_stats=None,
                )
            logger.info("  -> CACHE MISS: computing fresh result")

            # Cache miss - compute fresh
            stats = TraversalStats()
            limit_error = None
            try:
                result = self._compute_permission_with_limits(
                    subject_entity, permission, object_entity, tenant_id, stats, context
                )
            except GraphLimitExceeded as e:
                result = False
                limit_error = e

            self._cache_check_result_tenant_aware(
                subject_entity, permission, object_entity, tenant_id, result
            )

            decision_time_ms = (time.perf_counter() - start_time) * 1000
            stats.duration_ms = decision_time_ms

            return CheckResult(
                allowed=result,
                consistency_token=self._get_version_token(tenant_id),
                decision_time_ms=decision_time_ms,
                cached=False,
                cache_age_ms=None,
                traversal_stats=stats,
                indeterminate=limit_error is not None,
                limit_exceeded=limit_error,
            )

    def _compute_permission_with_limits(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tenant_id: str,
        stats: TraversalStats,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Compute permission with graph limits enforced (P0-5).

        Args:
            subject: Subject entity
            permission: Permission to check
            obj: Object entity
            tenant_id: Tenant ID
            stats: Traversal statistics
            context: Optional ABAC context

        Raises:
            GraphLimitExceeded: If any limit is exceeded during traversal
        """
        start_time = time.perf_counter()

        result = self._compute_permission_tenant_aware_with_limits(
            subject=subject,
            permission=permission,
            obj=obj,
            tenant_id=tenant_id,
            visited=set(),
            depth=0,
            start_time=start_time,
            stats=stats,
            context=context,
        )

        return result

    def _compute_permission_tenant_aware_with_limits(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tenant_id: str,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        start_time: float,
        stats: TraversalStats,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Compute permission with P0-5 limits enforced at each step."""

        # DEBUG: Add detailed logging
        import logging

        logger = logging.getLogger(__name__)
        indent = "  " * depth
        logger.info(
            f"{indent}→ [ENTER depth={depth}] CHECK: {subject.entity_type}:{subject.entity_id} has '{permission}' on {obj.entity_type}:{obj.entity_id}?"
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

        # Check for cycles
        visit_key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        if visit_key in visited:
            logger.debug(f"{indent}← CYCLE DETECTED, returning False")
            return False
        visited.add(visit_key)
        stats.nodes_visited += 1

        # P0-5: Check visited nodes limit
        if self.enable_graph_limits and len(visited) > GraphLimits.MAX_VISITED_NODES:
            raise GraphLimitExceeded("nodes", GraphLimits.MAX_VISITED_NODES, len(visited))

        # Get namespace config
        namespace = self.get_namespace(obj.entity_type)
        if not namespace:
            logger.debug(f"{indent}  No namespace for {obj.entity_type}, checking direct relation")
            stats.queries += 1
            if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
            result = self._has_direct_relation_tenant_aware(
                subject, permission, obj, tenant_id, context
            )
            logger.debug(f"{indent}← RESULT: {result}")
            return result

        # FIX: Check if permission is a mapped permission (e.g., "write" -> ["editor", "owner"])
        # If permission has usersets defined, check if subject has any of those relations
        if namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            if usersets:
                logger.info(
                    f"{indent}[depth={depth}] Permission '{permission}' maps to relations: {usersets} for {obj}"
                )
                # Permission is defined as a mapping to relations (e.g., write -> [editor, owner])
                # Check if subject has ANY of the relations that grant this permission
                for relation in usersets:
                    logger.info(
                        f"{indent}[depth={depth}]   Checking if {subject} has relation '{relation}'"
                    )
                    result = self._compute_permission_tenant_aware_with_limits(
                        subject,
                        relation,
                        obj,
                        tenant_id,
                        visited.copy(),
                        depth + 1,
                        start_time,
                        stats,
                        context,
                    )
                    logger.info(f"{indent}[depth={depth}]   → Result for '{relation}': {result}")
                    if result:
                        logger.info(f"{indent}[depth={depth}] ✅ GRANTED (via '{relation}')")
                        return True
                logger.info(f"{indent}[depth={depth}] ❌ DENIED (no relations granted access)")
                return False

        # If permission is not mapped, try as a direct relation
        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
            logger.debug(
                f"{indent}  No relation config for '{permission}', checking direct relation"
            )
            stats.queries += 1
            if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
            result = self._has_direct_relation_tenant_aware(
                subject, permission, obj, tenant_id, context
            )
            logger.debug(f"{indent}← RESULT: {result}")
            return result

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            logger.info(
                f"{indent}[depth={depth}] Relation '{permission}' is union of: {union_relations}"
            )

            # P0-5: Check fan-out limit
            if self.enable_graph_limits and len(union_relations) > GraphLimits.MAX_FAN_OUT:
                raise GraphLimitExceeded("fan_out", GraphLimits.MAX_FAN_OUT, len(union_relations))

            for i, rel in enumerate(union_relations):
                logger.info(
                    f"{indent}[depth={depth}]   [{i + 1}/{len(union_relations)}] Checking union member '{rel}'..."
                )
                try:
                    result = self._compute_permission_tenant_aware_with_limits(
                        subject,
                        rel,
                        obj,
                        tenant_id,
                        visited.copy(),
                        depth + 1,
                        start_time,
                        stats,
                        context,
                    )
                    logger.info(
                        f"{indent}[depth={depth}]   [{i + 1}/{len(union_relations)}] Result for '{rel}': {result}"
                    )
                    if result:
                        logger.info(f"{indent}[depth={depth}] ✅ GRANTED via union member '{rel}'")
                        return True
                except GraphLimitExceeded as e:
                    logger.error(
                        f"{indent}[depth={depth}]   [{i + 1}/{len(union_relations)}] GraphLimitExceeded while checking '{rel}': limit_type={e.limit_type}, limit_value={e.limit_value}, actual_value={e.actual_value}"
                    )
                    # Re-raise to propagate to caller
                    raise
                except Exception as e:
                    logger.error(
                        f"{indent}[depth={depth}]   [{i + 1}/{len(union_relations)}] Unexpected exception while checking '{rel}': {type(e).__name__}: {e}"
                    )
                    # Re-raise to maintain error handling semantics
                    raise
            logger.info(f"{indent}[depth={depth}] ❌ DENIED - no union members granted access")
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]
                logger.info(
                    f"{indent}[depth={depth}] Relation '{permission}' uses tupleToUserset: find via '{tupleset_relation}', check '{computed_userset}' on them"
                )

                # Find all objects related via tupleset (tenant-scoped)
                stats.queries += 1
                if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                    raise GraphLimitExceeded(
                        "queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries
                    )

                related_objects = self._find_related_objects_tenant_aware(
                    obj, tupleset_relation, tenant_id
                )
                logger.info(
                    f"{indent}[depth={depth}]   Found {len(related_objects)} related objects: {[f'{o.entity_type}:{o.entity_id}' for o in related_objects]}"
                )

                # P0-5: Check fan-out limit
                if self.enable_graph_limits and len(related_objects) > GraphLimits.MAX_FAN_OUT:
                    raise GraphLimitExceeded(
                        "fan_out", GraphLimits.MAX_FAN_OUT, len(related_objects)
                    )

                # Check if subject has computed_userset on any related object
                for related_obj in related_objects:
                    logger.debug(
                        f"{indent}  Checking '{computed_userset}' on related object {related_obj.entity_type}:{related_obj.entity_id}"
                    )
                    if self._compute_permission_tenant_aware_with_limits(
                        subject,
                        computed_userset,
                        related_obj,
                        tenant_id,
                        visited.copy(),
                        depth + 1,
                        start_time,
                        stats,
                        context,
                    ):
                        logger.debug(
                            f"{indent}← RESULT: True (via tupleToUserset on {related_obj.entity_type}:{related_obj.entity_id})"
                        )
                        return True

            logger.debug(f"{indent}← RESULT: False (tupleToUserset found no access)")
            return False

        # Direct relation check
        logger.info(f"{indent}[depth={depth}] Checking direct relation (fallback)")
        stats.queries += 1
        if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
            raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
        result = self._has_direct_relation_tenant_aware(
            subject, permission, obj, tenant_id, context
        )
        logger.info(f"{indent}← [EXIT depth={depth}] Direct relation result: {result}")
        return result

    def _find_related_objects_tenant_aware(
        self, obj: Entity, relation: str, tenant_id: str
    ) -> list[Entity]:
        """Find all objects related to obj via relation (tenant-scoped).

        Args:
            obj: Object entity
            relation: Relation type
            tenant_id: Tenant ID to scope the query

        Returns:
            List of related object entities within the tenant
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"_find_related_objects_tenant_aware: obj={obj}, relation={relation}, tenant_id={tenant_id}"
        )

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
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND tenant_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    obj.entity_type,
                    obj.entity_id,
                    relation,
                    tenant_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                results.append(Entity(row["object_type"], row["object_id"]))

            logger.info(
                f"_find_related_objects_tenant_aware: Found {len(results)} objects for {obj} via '{relation}': {[str(r) for r in results]}"
            )
            return results

    def _has_direct_relation_tenant_aware(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        tenant_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check if subject has direct relation to object (tenant-scoped).

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            tenant_id: Tenant ID to scope the query
            context: Optional ABAC context for condition evaluation

        Returns:
            True if direct relation exists within the tenant
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Check for direct concrete subject tuple (with ABAC conditions support)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT tuple_id, conditions FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND tenant_id = ?
                      AND subject_relation IS NULL
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    subject.entity_type,
                    subject.entity_id,
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    tenant_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            row = cursor.fetchone()
            if row:
                # Tuple exists - check conditions if context provided
                conditions_json = row["conditions"]

                if conditions_json:
                    try:
                        import json

                        conditions = (
                            json.loads(conditions_json)
                            if isinstance(conditions_json, str)
                            else conditions_json
                        )
                        # Evaluate ABAC conditions
                        if not self._evaluate_conditions(conditions, context):
                            # Conditions not satisfied
                            pass  # Continue to check userset-as-subject
                        else:
                            return True  # Conditions satisfied
                    except (json.JSONDecodeError, TypeError):
                        # On parse error, treat as no conditions (allow)
                        return True
                else:
                    return True  # No conditions, allow

            # Check for userset-as-subject tuple (e.g., group#member)
            # Find all tuples where object is our target and subject is a userset
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, subject_relation
                    FROM rebac_tuples
                    WHERE relation = ?
                      AND object_type = ? AND object_id = ?
                      AND subject_relation IS NOT NULL
                      AND tenant_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    tenant_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            # BUGFIX (Issue #1): Use recursive ReBAC evaluation instead of direct SQL
            # This ensures nested groups, unions, and tupleToUserset work correctly
            # For each userset (e.g., group:eng#member), recursively check if subject
            # has the userset_relation (e.g., "member") on the userset entity (e.g., group:eng)
            for row in cursor.fetchall():
                userset_type = row["subject_type"]
                userset_id = row["subject_id"]
                userset_relation = row["subject_relation"]

                # Recursive check: Does subject have userset_relation on the userset entity?
                # This handles nested groups, union expansion, etc.
                # NOTE: We create a fresh stats object for this sub-check to avoid
                # conflating limits across different code paths
                from nexus.core.rebac_manager_enhanced import TraversalStats

                sub_stats = TraversalStats()
                userset_entity = Entity(userset_type, userset_id)

                # Use a bounded sub-check to prevent infinite recursion
                # We inherit the same visited set to detect cycles across the full graph
                try:
                    if self._compute_permission_tenant_aware_with_limits(
                        subject=subject,
                        permission=userset_relation,
                        obj=userset_entity,
                        tenant_id=tenant_id,
                        visited=set(),  # Fresh visited set for this sub-check
                        depth=0,  # Reset depth for sub-check
                        start_time=time.perf_counter(),  # Fresh timer
                        stats=sub_stats,
                        context=context,
                    ):
                        return True
                except GraphLimitExceeded:
                    # If userset check hits limits, skip this userset and try others
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.warning(
                        f"Userset check hit limits: {subject} -> {userset_relation} -> {userset_entity}, skipping"
                    )
                    continue

            return False

    def _get_version_token(self, tenant_id: str = "default") -> str:
        """Get current version token (P0-1).

        BUGFIX (Issue #2): Use DB-backed per-tenant sequence instead of in-memory counter.
        This ensures version tokens are:
        - Monotonic across process restarts
        - Consistent across multiple processes/replicas
        - Scoped per-tenant for proper isolation

        Args:
            tenant_id: Tenant ID to get version for

        Returns:
            Monotonic version token string (e.g., "v123")
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # PostgreSQL: Use atomic UPDATE ... RETURNING for increment-and-fetch
            # SQLite: Use SELECT + UPDATE (less efficient but works)
            if self.engine.dialect.name == "postgresql":
                # Atomic increment-and-return
                cursor.execute(
                    """
                    INSERT INTO rebac_version_sequences (tenant_id, current_version, updated_at)
                    VALUES (%s, 1, NOW())
                    ON CONFLICT (tenant_id)
                    DO UPDATE SET current_version = rebac_version_sequences.current_version + 1,
                                  updated_at = NOW()
                    RETURNING current_version
                    """,
                    (tenant_id,),
                )
                row = cursor.fetchone()
                version = row["current_version"] if row else 1
            else:
                # SQLite: Two-step increment
                cursor.execute(
                    self._fix_sql_placeholders(
                        "SELECT current_version FROM rebac_version_sequences WHERE tenant_id = ?"
                    ),
                    (tenant_id,),
                )
                row = cursor.fetchone()

                if row:
                    current = row["current_version"]
                    new_version = current + 1
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            UPDATE rebac_version_sequences
                            SET current_version = ?, updated_at = ?
                            WHERE tenant_id = ?
                            """
                        ),
                        (new_version, datetime.now(UTC).isoformat(), tenant_id),
                    )
                else:
                    # First version for this tenant
                    new_version = 1
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            INSERT INTO rebac_version_sequences (tenant_id, current_version, updated_at)
                            VALUES (?, ?, ?)
                            """
                        ),
                        (tenant_id, new_version, datetime.now(UTC).isoformat()),
                    )

                version = new_version

            conn.commit()
            return f"v{version}"

    def _get_cached_check_tenant_aware_bounded(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tenant_id: str,
        max_age_seconds: float,
    ) -> bool | None:
        """Get cached result with bounded staleness (P0-1).

        Returns None if cache entry is older than max_age_seconds.
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            min_computed_at = datetime.now(UTC) - timedelta(seconds=max_age_seconds)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT result, computed_at, expires_at
                    FROM rebac_check_cache
                    WHERE tenant_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                      AND computed_at >= ?
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
                    min_computed_at.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )

            row = cursor.fetchone()
            if row:
                result = row["result"]
                return bool(result)
            return None

    def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        tenant_id: str,
        consistency: ConsistencyLevel = ConsistencyLevel.EVENTUAL,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        """Check permissions for multiple (subject, permission, object) tuples in batch.

        This is a performance optimization for list operations that need to check
        permissions on many objects. Instead of making N individual rebac_check() calls
        (each with 10-15 DB queries), this method:
        1. Fetches all relevant tuples in 1-2 queries
        2. Builds an in-memory permission graph
        3. Runs permission checks against the cached graph
        4. Returns all results in a single call

        Performance impact: 100x reduction in database queries for N=20 objects.
        - Before: 20 files × 15 queries/file = 300 queries
        - After: 1-2 queries to fetch all tuples + in-memory computation

        Args:
            checks: List of (subject, permission, object) tuples to check
                Example: [(("user", "alice"), "read", ("file", "/doc.txt")),
                          (("user", "alice"), "read", ("file", "/data.csv"))]
            tenant_id: Tenant ID to scope all checks
            consistency: Consistency level (EVENTUAL, BOUNDED, STRONG)

        Returns:
            Dict mapping each check tuple to its result (True if allowed, False if denied)
            Example: {(("user", "alice"), "read", ("file", "/doc.txt")): True, ...}

        Example:
            >>> manager = EnhancedReBACManager(engine)
            >>> checks = [
            ...     (("user", "alice"), "read", ("file", "/workspace/a.txt")),
            ...     (("user", "alice"), "read", ("file", "/workspace/b.txt")),
            ...     (("user", "alice"), "read", ("file", "/workspace/c.txt")),
            ... ]
            >>> results = manager.rebac_check_bulk(checks, tenant_id="org_123")
            >>> # Returns: {check1: True, check2: True, check3: False}
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"rebac_check_bulk: Checking {len(checks)} permissions in batch")

        if not checks:
            return {}

        # If tenant isolation is disabled, fall back to individual checks
        # (bulk optimization requires tenant-aware queries)
        if not self.enforce_tenant_isolation:
            logger.warning(
                "rebac_check_bulk called with tenant isolation disabled, falling back to individual checks"
            )
            results = {}
            for check in checks:
                subject, permission, obj = check
                results[check] = self.rebac_check(subject, permission, obj, tenant_id=tenant_id)
            return results

        # Validate tenant_id (same logic as rebac_check)
        if not tenant_id:
            import os

            is_production = (
                os.getenv("NEXUS_ENV") == "production" or os.getenv("ENVIRONMENT") == "production"
            )
            if is_production:
                raise ValueError("tenant_id is required for bulk permission checks in production")
            else:
                logger.warning("rebac_check_bulk called without tenant_id, defaulting to 'default'")
                tenant_id = "default"

        # STRATEGY: For EVENTUAL consistency, check cache first for all checks
        # For any cache misses, fetch all relevant tuples in bulk and compute
        results = {}
        cache_misses = []

        # OPTIMIZATION: Skip cache check for bulk operations (adds 323ms overhead)
        # For large batches (>100), cache checking is slower than just computing with Rust
        # TODO: Implement batch cache lookup for better performance
        if len(checks) > 100:
            logger.info(
                f"[CACHE-OPT] Skipping cache check for {len(checks)} items (would add ~{len(checks) * 0.5:.0f}ms)"
            )
            cache_misses = checks
        elif consistency == ConsistencyLevel.EVENTUAL:
            # Try to get results from cache first (only for small batches)
            for check in checks:
                subject, permission, obj = check
                subject_entity = Entity(subject[0], subject[1])
                obj_entity = Entity(obj[0], obj[1])

                cached = self._get_cached_check_tenant_aware(
                    subject_entity, permission, obj_entity, tenant_id
                )
                if cached is not None:
                    results[check] = cached
                    logger.debug(f"Cache HIT for {check}")
                else:
                    cache_misses.append(check)
                    logger.debug(f"Cache MISS for {check}")
        else:
            # For BOUNDED/STRONG consistency, skip cache
            cache_misses = checks

        if not cache_misses:
            logger.info("All checks satisfied from cache")
            return results

        logger.info(f"Cache misses: {len(cache_misses)}, fetching tuples in bulk")

        # PHASE 1: Fetch all relevant tuples in bulk
        # Extract all unique subjects and objects from cache misses
        all_subjects = set()
        all_objects = set()
        for check in cache_misses:
            subject, permission, obj = check
            all_subjects.add(subject)
            all_objects.add(obj)

        # For file paths, we also need to fetch parent hierarchy tuples
        # Example: checking /a/b/c.txt requires parent tuples: (c.txt, parent, b), (b, parent, a), etc.
        file_paths = []
        for obj_type, obj_id in all_objects:
            if obj_type == "file" and "/" in obj_id:
                file_paths.append(obj_id)

        # Fetch all tuples involving these subjects/objects in a single query
        # This is the key optimization: instead of N queries, we make 1-2 queries
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Build query with OR conditions for each subject/object
            # Query 1: Get all tuples where subject or object is in our set
            placeholders_subjects = ", ".join(["(?, ?)"] * len(all_subjects))
            placeholders_objects = ", ".join(["(?, ?)"] * len(all_objects))

            # Flatten subject/object tuples for SQL parameters
            subject_params = []
            for subj_type, subj_id in all_subjects:
                subject_params.extend([subj_type, subj_id])

            object_params = []
            for obj_type, obj_id in all_objects:
                object_params.extend([obj_type, obj_id])

            # OPTIMIZATION: For file paths, also fetch parent hierarchy tuples in bulk
            # This ensures we have all parent tuples needed for parent_owner/parent_editor/parent_viewer checks
            # Without this, we'd miss tuples like (child, "parent", parent) that aren't directly in our object set
            file_path_conditions = []
            file_path_params = []
            for file_path in file_paths:
                # Fetch all parent tuples for files that are prefixes of this path
                # Example: for /a/b/c.txt, fetch tuples where subject_id LIKE '/a/%'
                # This captures: (c.txt, parent, b), (b, parent, a), (a, parent, /)
                # Use LIKE to match path prefixes
                path_prefix = file_path.rsplit("/", 1)[0] if "/" in file_path else "/"
                if path_prefix:
                    # Match files under this path (including the file itself)
                    file_path_conditions.append("(subject_type = ? AND subject_id LIKE ?)")
                    file_path_params.extend(["file", file_path + "%"])
                    # Also match parent paths
                    if path_prefix != "/":
                        file_path_conditions.append("(subject_type = ? AND subject_id LIKE ?)")
                        file_path_params.extend(["file", path_prefix + "%"])

            # Build full query
            where_clauses = [
                f"(subject_type, subject_id) IN ({placeholders_subjects})",
                f"(object_type, object_id) IN ({placeholders_objects})",
            ]
            if file_path_conditions:
                # Limit file path expansion to avoid too many results
                # Only add top-level path patterns (not all nested paths)
                where_clauses.append(f"({' OR '.join(file_path_conditions[:10])})")

            query = self._fix_sql_placeholders(
                f"""
                SELECT subject_type, subject_id, subject_relation, relation,
                       object_type, object_id, conditions, expires_at
                FROM rebac_tuples
                WHERE tenant_id = ?
                  AND (expires_at IS NULL OR expires_at >= ?)
                  AND ({" OR ".join(where_clauses)})
                """
            )

            params = (
                [tenant_id, datetime.now(UTC).isoformat()]
                + subject_params
                + object_params
                + file_path_params[:20]  # Limit params to avoid query size explosion
            )
            cursor.execute(query, params)

            # Build in-memory graph of all tuples
            tuples_graph = []
            for row in cursor.fetchall():
                tuples_graph.append(
                    {
                        "subject_type": row["subject_type"],
                        "subject_id": row["subject_id"],
                        "subject_relation": row["subject_relation"],
                        "relation": row["relation"],
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                        "conditions": row["conditions"],
                        "expires_at": row["expires_at"],
                    }
                )

            logger.info(
                f"Fetched {len(tuples_graph)} tuples in bulk for graph computation (includes parent hierarchy)"
            )

        # PHASE 2: Compute permissions for each cache miss using the in-memory graph
        # This avoids additional DB queries per check
        #
        # OPTIMIZATION: Create a shared memoization cache for this bulk operation
        # This dramatically speeds up repeated checks like:
        # - Checking if admin owns /workspace (used by all 679 files via parent_owner)
        # - Checking if user is in a group (used by all group members)
        # Without memo: 679 files × 10 checks each = 6,790 computations
        # With memo: ~100-200 unique computations (rest are cache hits)
        # Use a list to track hit count (mutable so inner function can modify it)
        bulk_memo_cache: dict[tuple[str, str, str, str, str], bool] = {}
        memo_stats = {
            "hits": 0,
            "misses": 0,
            "max_depth": 0,
        }  # Track cache hits/misses and max depth

        logger.info(
            f"Starting computation for {len(cache_misses)} cache misses with shared memo cache"
        )

        # Log the first permission expansion to verify hybrid schema is being used
        if cache_misses:
            first_check = cache_misses[0]
            subject, permission, obj = first_check
            # obj is a tuple (entity_type, entity_id), not an Entity
            obj_type = obj[0]
            namespace = self.get_namespace(obj_type)
            if namespace and namespace.has_permission(permission):
                usersets = namespace.get_permission_usersets(permission)
                logger.info(
                    f"[SCHEMA-VERIFY] Permission '{permission}' on '{obj_type}' expands to {len(usersets)} relations: {usersets}"
                )
                logger.info(
                    "[SCHEMA-VERIFY] Expected: 3 for hybrid schema (viewer, editor, owner) or 9 for flattened"
                )

        # TRY RUST ACCELERATION FIRST for bulk computation
        from nexus.core.rebac_fast import check_permissions_bulk_with_fallback, is_rust_available

        rust_success = False
        if is_rust_available() and len(cache_misses) >= 10:
            try:
                logger.info(f"⚡ Attempting Rust acceleration for {len(cache_misses)} checks")

                # Get all namespace configs
                object_types = {obj[0] for _, _, obj in cache_misses}
                namespace_configs = {}
                for obj_type in object_types:
                    ns = self.get_namespace(obj_type)
                    if ns:
                        # ns.config contains the relations and permissions
                        namespace_configs[obj_type] = ns.config

                # Debug: log the config format
                if namespace_configs:
                    sample_type = list(namespace_configs.keys())[0]
                    sample_config = namespace_configs[sample_type]
                    logger.info(
                        f"[RUST-DEBUG] Sample namespace config for '{sample_type}': {str(sample_config)[:200]}"
                    )

                # Call Rust for bulk computation
                import time

                rust_start = time.perf_counter()
                rust_results_dict = check_permissions_bulk_with_fallback(
                    cache_misses, tuples_graph, namespace_configs, force_python=False
                )
                rust_elapsed = time.perf_counter() - rust_start
                per_check_us = (rust_elapsed / len(cache_misses)) * 1_000_000
                logger.info(
                    f"[RUST-TIMING] {len(cache_misses)} checks in {rust_elapsed * 1000:.1f}ms = {per_check_us:.1f}µs/check"
                )

                # Convert results (skip caching in loop for speed)
                for check in cache_misses:
                    subject, permission, obj = check
                    key = (subject[0], subject[1], permission, obj[0], obj[1])
                    result = rust_results_dict.get(key, False)
                    results[check] = result

                # Cache results in batch after loop (avoid 679 individual cache writes)
                # TODO: Implement batch cache write for better performance
                # For now, skip caching to avoid the 425ms overhead
                logger.info(
                    f"[RUST-PERF] Skipping individual cache writes (would add ~{len(cache_misses) * 0.6:.0f}ms overhead)"
                )

                rust_success = True
                logger.info(f"✅ Rust acceleration successful for {len(cache_misses)} checks")

            except Exception as e:
                logger.warning(f"Rust acceleration failed: {e}, falling back to Python")
                rust_success = False

        # FALLBACK TO PYTHON if Rust not available or failed
        if not rust_success:
            logger.info(f"🐍 Using Python for {len(cache_misses)} checks")
            for check in cache_misses:
                subject, permission, obj = check
                subject_entity = Entity(subject[0], subject[1])
                obj_entity = Entity(obj[0], obj[1])

                # Compute permission using the pre-fetched tuples_graph
                # For now, fall back to regular check (will be optimized in follow-up)
                # This already provides 90% of the benefit by reducing tuple fetch queries
                try:
                    result = self._compute_permission_bulk_helper(
                        subject_entity,
                        permission,
                        obj_entity,
                        tenant_id,
                        tuples_graph,
                        bulk_memo_cache=bulk_memo_cache,  # Pass shared memo cache
                        memo_stats=memo_stats,  # Pass stats tracker
                    )
                except Exception as e:
                    logger.warning(f"Bulk check failed for {check}, falling back: {e}")
                    # Fallback to individual check
                    result = self.rebac_check(
                        subject, permission, obj, tenant_id=tenant_id, consistency=consistency
                    )

                results[check] = result

                # Cache the result if using EVENTUAL consistency
                if consistency == ConsistencyLevel.EVENTUAL:
                    self._cache_check_result(
                        subject_entity, permission, obj_entity, result, tenant_id
                    )

        # Report actual cache statistics
        total_accesses = memo_stats["hits"] + memo_stats["misses"]
        hit_rate = (memo_stats["hits"] / total_accesses * 100) if total_accesses > 0 else 0

        logger.info(f"Bulk memo cache stats: {len(bulk_memo_cache)} unique checks stored")
        logger.info(
            f"Cache performance: {memo_stats['hits']} hits + {memo_stats['misses']} misses = {total_accesses} total accesses"
        )
        logger.info(f"Cache hit rate: {hit_rate:.1f}% ({memo_stats['hits']}/{total_accesses})")
        logger.info(f"Max traversal depth reached: {memo_stats.get('max_depth', 0)}")

        logger.info(f"rebac_check_bulk completed: {len(results)} results")
        return results

    def _compute_permission_bulk_helper(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tenant_id: str,
        tuples_graph: list[dict[str, Any]],
        depth: int = 0,
        visited: set[tuple[str, str, str, str, str]] | None = None,
        bulk_memo_cache: dict[tuple[str, str, str, str, str], bool] | None = None,
        memo_stats: dict[str, int] | None = None,
    ) -> bool:
        """Compute permission using pre-fetched tuples graph with full in-memory traversal.

        This implements the complete ReBAC graph traversal algorithm without additional DB queries.
        Handles: direct relations, union, intersection, exclusion, tupleToUserset (parent/group inheritance).

        Args:
            subject: Subject entity
            permission: Permission to check
            obj: Object entity
            tenant_id: Tenant ID
            tuples_graph: Pre-fetched list of all relevant tuples
            depth: Current traversal depth (for cycle detection)
            visited: Set of visited nodes (for cycle detection)
            bulk_memo_cache: Shared memoization cache for bulk operations (optimization)

        Returns:
            True if permission is granted
        """
        import logging

        logger = logging.getLogger(__name__)

        # Initialize visited set on first call
        if visited is None:
            visited = set()

        # OPTIMIZATION: Check memoization cache first
        # This avoids recomputing the same permission checks multiple times within a bulk operation
        # Example: All 679 files check "does admin own /workspace?" - only compute once!
        memo_key = (
            subject.entity_type,
            subject.entity_id,
            permission,
            obj.entity_type,
            obj.entity_id,
        )
        if bulk_memo_cache is not None and memo_key in bulk_memo_cache:
            # Cache hit! Return cached result
            if memo_stats is not None:
                memo_stats["hits"] += 1
                # Log every 100th hit to show progress without flooding
                if memo_stats["hits"] % 100 == 0:
                    logger.info(
                        f"[MEMO HIT #{memo_stats['hits']}] {subject.entity_type}:{subject.entity_id} {permission} on {obj.entity_type}:{obj.entity_id}"
                    )
            return bulk_memo_cache[memo_key]

        # Cache miss - will need to compute
        if memo_stats is not None:
            memo_stats["misses"] += 1
            # Track maximum depth reached
            if depth > memo_stats.get("max_depth", 0):
                memo_stats["max_depth"] = depth

        # Depth limit check (prevent infinite recursion)
        MAX_DEPTH = 50
        if depth > MAX_DEPTH:
            logger.warning(
                f"_compute_permission_bulk_helper: Depth limit exceeded ({depth} > {MAX_DEPTH}), denying"
            )
            return False

        # Cycle detection (within this specific traversal path)
        visit_key = memo_key  # Same key works for both
        if visit_key in visited:
            logger.debug(f"_compute_permission_bulk_helper: Cycle detected at {visit_key}, denying")
            return False
        visited.add(visit_key)

        # Get namespace config
        namespace = self.get_namespace(obj.entity_type)
        if not namespace:
            # No namespace, check for direct relation
            return self._check_direct_relation_in_graph(subject, permission, obj, tuples_graph)

        # P0-1: Check if permission is defined via "permissions" config
        # Example: "read" -> ["viewer", "editor", "owner"]
        if namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            logger.debug(
                f"_compute_permission_bulk_helper [depth={depth}]: Permission '{permission}' expands to usersets: {usersets}"
            )
            # Check each userset in union (OR semantics)
            result = False
            for userset in usersets:
                if self._compute_permission_bulk_helper(
                    subject,
                    userset,
                    obj,
                    tenant_id,
                    tuples_graph,
                    depth + 1,
                    visited.copy(),
                    bulk_memo_cache,
                    memo_stats,
                ):
                    result = True
                    break
            # Store result in memo cache before returning
            if bulk_memo_cache is not None:
                bulk_memo_cache[memo_key] = result
            return result

        # Handle union (OR of multiple relations)
        # Example: "owner" -> union: ["direct_owner", "parent_owner", "group_owner"]
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            logger.debug(
                f"_compute_permission_bulk_helper [depth={depth}]: Union '{permission}' -> {union_relations}"
            )
            result = False
            for rel in union_relations:
                if self._compute_permission_bulk_helper(
                    subject,
                    rel,
                    obj,
                    tenant_id,
                    tuples_graph,
                    depth + 1,
                    visited.copy(),
                    bulk_memo_cache,
                    memo_stats,
                ):
                    result = True
                    break
            # Store result in memo cache before returning
            if bulk_memo_cache is not None:
                bulk_memo_cache[memo_key] = result
            return result

        # Handle intersection (AND of multiple relations)
        if namespace.has_intersection(permission):
            intersection_relations = namespace.get_intersection_relations(permission)
            logger.debug(
                f"_compute_permission_bulk_helper [depth={depth}]: Intersection '{permission}' -> {intersection_relations}"
            )
            result = True
            for rel in intersection_relations:
                if not self._compute_permission_bulk_helper(
                    subject,
                    rel,
                    obj,
                    tenant_id,
                    tuples_graph,
                    depth + 1,
                    visited.copy(),
                    bulk_memo_cache,
                    memo_stats,
                ):
                    result = False
                    break  # If any is false, whole intersection is false
            # Store result in memo cache before returning
            if bulk_memo_cache is not None:
                bulk_memo_cache[memo_key] = result
            return result

        # Handle exclusion (NOT relation)
        if namespace.has_exclusion(permission):
            excluded_rel = namespace.get_exclusion_relation(permission)
            if excluded_rel:
                logger.debug(
                    f"_compute_permission_bulk_helper [depth={depth}]: Exclusion '{permission}' NOT {excluded_rel}"
                )
                result = not self._compute_permission_bulk_helper(
                    subject,
                    excluded_rel,
                    obj,
                    tenant_id,
                    tuples_graph,
                    depth + 1,
                    visited.copy(),
                    bulk_memo_cache,
                    memo_stats,
                )
                # Store result in memo cache before returning
                if bulk_memo_cache is not None:
                    bulk_memo_cache[memo_key] = result
                return result
            return False

        # Handle tupleToUserset (indirect relation via another object)
        # This is the KEY fix for parent/group inheritance performance!
        # Example: parent_owner -> tupleToUserset: {tupleset: "parent", computedUserset: "owner"}
        # Meaning: Check if subject has "owner" permission on any parent of obj
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            logger.debug(
                f"_compute_permission_bulk_helper [depth={depth}]: tupleToUserset '{permission}' -> {ttu}"
            )
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]

                # Find related objects via tupleset IN MEMORY (no DB query!)
                related_objects = self._find_related_objects_in_graph(
                    obj, tupleset_relation, tuples_graph
                )
                logger.debug(
                    f"_compute_permission_bulk_helper [depth={depth}]: Found {len(related_objects)} related objects via '{tupleset_relation}'"
                )

                # Check if subject has computed_userset on any related object
                result = False
                for related_obj in related_objects:
                    if self._compute_permission_bulk_helper(
                        subject,
                        computed_userset,
                        related_obj,
                        tenant_id,
                        tuples_graph,
                        depth + 1,
                        visited.copy(),
                        bulk_memo_cache,
                        memo_stats,
                    ):
                        logger.debug(
                            f"_compute_permission_bulk_helper [depth={depth}]: GRANTED via tupleToUserset through {related_obj}"
                        )
                        result = True
                        break

                logger.debug(
                    f"_compute_permission_bulk_helper [depth={depth}]: No related objects granted permission"
                )
                # Store result in memo cache before returning
                if bulk_memo_cache is not None:
                    bulk_memo_cache[memo_key] = result
                return result
            return False

        # Direct relation check (base case)
        result = self._check_direct_relation_in_graph(subject, permission, obj, tuples_graph)
        # Store result in memo cache before returning
        if bulk_memo_cache is not None:
            bulk_memo_cache[memo_key] = result
        return result

    def _check_direct_relation_in_graph(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tuples_graph: list[dict[str, Any]],
    ) -> bool:
        """Check if a direct relation tuple exists in the pre-fetched graph.

        Args:
            subject: Subject entity
            permission: Relation name
            obj: Object entity
            tuples_graph: Pre-fetched tuples

        Returns:
            True if direct tuple exists
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
                # TODO: Check conditions and expiry if needed
                return True
        return False

    def _find_related_objects_in_graph(
        self,
        obj: Entity,
        tupleset_relation: str,
        tuples_graph: list[dict[str, Any]],
    ) -> list[Entity]:
        """Find all objects related to obj via tupleset_relation in the pre-fetched graph.

        This is used for tupleToUserset traversal. For example:
        - To find parent directories: look for tuples (child, "parent", parent)
        - To find group memberships: look for tuples (subject, "member", group)

        Args:
            obj: Object to find relations for
            tupleset_relation: Relation name (e.g., "parent", "member")
            tuples_graph: Pre-fetched tuples

        Returns:
            List of related Entity objects
        """
        related = []
        for tuple_data in tuples_graph:
            # For parent inheritance: (child, "parent", parent)
            # obj is the child, we want to find parents
            if (
                tuple_data["subject_type"] == obj.entity_type
                and tuple_data["subject_id"] == obj.entity_id
                and tuple_data["relation"] == tupleset_relation
            ):
                # The object of this tuple is the related entity
                related.append(Entity(tuple_data["object_type"], tuple_data["object_id"]))

            # For group inheritance: subject is implicit (we're looking at obj's permissions)
            # But the tuple structure is: (group#member, "direct_owner", file)
            # Where we need to check: (user, "member", group) separately
            # This is handled by the computed_userset check, so we only need the above case

        return related
