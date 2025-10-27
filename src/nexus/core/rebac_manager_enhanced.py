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

    MAX_DEPTH = 10  # Max recursion depth
    MAX_FAN_OUT = 1000  # Max edges per union/expand
    MAX_EXECUTION_TIME_MS = 100  # Hard timeout (100ms)
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
        max_depth: int = 10,
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
        # If tenant isolation is disabled, use base ReBACManager implementation
        if not self.enforce_tenant_isolation:
            from nexus.core.rebac_manager import ReBACManager

            return ReBACManager.rebac_check(self, subject, permission, object, context, tenant_id)

        result = self.rebac_check_detailed(
            subject, permission, object, context, tenant_id, consistency
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
            cached = self._get_cached_check_tenant_aware(
                subject_entity, permission, object_entity, tenant_id
            )
            if cached is not None:
                decision_time_ms = (time.perf_counter() - start_time) * 1000
                return CheckResult(
                    allowed=cached,
                    consistency_token=self._get_version_token(tenant_id),
                    decision_time_ms=decision_time_ms,
                    cached=True,
                    cache_age_ms=None,  # Could be up to cache_ttl_seconds old
                    traversal_stats=None,
                )

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
        logger.debug(
            f"{indent}→ CHECK: {subject.entity_type}:{subject.entity_id} has '{permission}' on {obj.entity_type}:{obj.entity_id}?"
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
                logger.debug(f"{indent}  Permission '{permission}' maps to relations: {usersets}")
                # Permission is defined as a mapping to relations (e.g., write -> [editor, owner])
                # Check if subject has ANY of the relations that grant this permission
                for relation in usersets:
                    logger.debug(
                        f"{indent}  Checking relation '{relation}' from permission mapping"
                    )
                    logger.debug(
                        f"{indent}  About to recursively check: {subject.entity_type}:{subject.entity_id} has '{relation}' on {obj.entity_type}:{obj.entity_id}"
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
                    logger.debug(f"{indent}  Recursive check returned: {result}")
                    if result:
                        logger.debug(
                            f"{indent}← RESULT: True (via permission mapping to '{relation}')"
                        )
                        return True
                logger.debug(
                    f"{indent}← RESULT: False (no relations from permission mapping granted access)"
                )
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
            logger.debug(f"{indent}  Relation '{permission}' is union of: {union_relations}")

            # P0-5: Check fan-out limit
            if self.enable_graph_limits and len(union_relations) > GraphLimits.MAX_FAN_OUT:
                raise GraphLimitExceeded("fan_out", GraphLimits.MAX_FAN_OUT, len(union_relations))

            for rel in union_relations:
                logger.debug(f"{indent}  Checking union member '{rel}'")
                if self._compute_permission_tenant_aware_with_limits(
                    subject,
                    rel,
                    obj,
                    tenant_id,
                    visited.copy(),
                    depth + 1,
                    start_time,
                    stats,
                    context,
                ):
                    logger.debug(f"{indent}← RESULT: True (via union member '{rel}')")
                    return True
            logger.debug(f"{indent}← RESULT: False (no union members granted access)")
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]
                logger.debug(
                    f"{indent}  Relation '{permission}' uses tupleToUserset: find objects via '{tupleset_relation}', check '{computed_userset}'"
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
                logger.debug(
                    f"{indent}  Found {len(related_objects)} related objects: {[f'{o.entity_type}:{o.entity_id}' for o in related_objects]}"
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
        logger.debug(f"{indent}  Checking direct relation (fallback)")
        stats.queries += 1
        if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
            raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
        result = self._has_direct_relation_tenant_aware(
            subject, permission, obj, tenant_id, context
        )
        logger.debug(f"{indent}← RESULT: {result}")
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
            cursor = conn.cursor()

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id
                    FROM rebac_tuples
                    WHERE object_type = ? AND object_id = ?
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
                if hasattr(row, "keys"):
                    results.append(Entity(row["subject_type"], row["subject_id"]))
                else:
                    results.append(Entity(row[0], row[1]))
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
            cursor = conn.cursor()

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
                conditions_json = row["conditions"] if hasattr(row, "keys") else row[1]

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
                if hasattr(row, "keys"):
                    userset_type = row["subject_type"]
                    userset_id = row["subject_id"]
                    userset_relation = row["subject_relation"]
                else:
                    userset_type = row[0]
                    userset_id = row[1]
                    userset_relation = row[2]

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
            cursor = conn.cursor()

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
                version = row[0] if row else 1
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
                    current = row[0] if isinstance(row, tuple) else row["current_version"]
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
            cursor = conn.cursor()

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
                result = row["result"] if hasattr(row, "keys") else row[0]
                return bool(result)
            return None
