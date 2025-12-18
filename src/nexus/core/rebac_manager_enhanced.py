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
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from nexus.core.rebac import CROSS_TENANT_ALLOWED_RELATIONS, Entity
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
    MAX_EXECUTION_TIME_MS = 1000  # 1 second timeout for permission computation
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

        # PERFORMANCE FIX: Cache tenant tuples to avoid O(T) fetch per permission check
        # Key: tenant_id, Value: (tuples_list, namespace_configs, cached_at_timestamp)
        # This dramatically reduces DB queries: from O(T) per check to O(1) amortized
        self._tenant_graph_cache: dict[str, tuple[list[dict[str, Any]], dict[str, Any], float]] = {}
        self._tenant_graph_cache_ttl = cache_ttl_seconds  # Reuse existing TTL

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
        logger.debug(
            f"EnhancedReBACManager.rebac_check called: enforce_tenant_isolation={self.enforce_tenant_isolation}, MAX_DEPTH={GraphLimits.MAX_DEPTH}"
        )

        # If tenant isolation is disabled, use base ReBACManager implementation
        if not self.enforce_tenant_isolation:
            from nexus.core.rebac_manager import ReBACManager

            logger.debug(f"  -> Falling back to base ReBACManager, base max_depth={self.max_depth}")
            return ReBACManager.rebac_check(self, subject, permission, object, context, tenant_id)

        logger.debug("  -> Using rebac_check_detailed")
        result = self.rebac_check_detailed(
            subject, permission, object, context, tenant_id, consistency
        )
        logger.debug(
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
                logger.debug(f"  -> CACHE HIT: returning cached result={cached}")
                decision_time_ms = (time.perf_counter() - start_time) * 1000
                return CheckResult(
                    allowed=cached,
                    consistency_token=self._get_version_token(tenant_id),
                    decision_time_ms=decision_time_ms,
                    cached=True,
                    cache_age_ms=None,  # Could be up to cache_ttl_seconds old
                    traversal_stats=None,
                )
            logger.debug("  -> CACHE MISS: computing fresh result")

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

        This method first tries to use Rust acceleration (which has proper memoization
        to prevent exponential recursion). If Rust is unavailable or fails, it falls
        back to the Python implementation.

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
        import logging

        logger = logging.getLogger(__name__)
        start_time = time.perf_counter()

        # Try Rust acceleration first (has proper memoization, prevents timeout)
        try:
            from nexus.core.rebac_fast import check_permission_single_rust, is_rust_available

            if is_rust_available():
                # Fetch tuples and namespace configs for Rust
                # CROSS-TENANT FIX: Pass subject to include cross-tenant shares
                tuples = self._fetch_tuples_for_rust(tenant_id, subject=subject)
                namespace_configs = self._get_namespace_configs_for_rust()

                result = check_permission_single_rust(
                    subject_type=subject.entity_type,
                    subject_id=subject.entity_id,
                    permission=permission,
                    object_type=obj.entity_type,
                    object_id=obj.entity_id,
                    tuples=tuples,
                    namespace_configs=namespace_configs,
                )

                elapsed_ms = (time.perf_counter() - start_time) * 1000
                stats.duration_ms = elapsed_ms
                logger.debug(
                    f"[RUST-SINGLE] Permission check completed in {elapsed_ms:.2f}ms: "
                    f"{subject.entity_type}:{subject.entity_id} {permission} "
                    f"{obj.entity_type}:{obj.entity_id} = {result}"
                )
                return result

        except Exception as e:
            logger.warning(f"Rust single permission check failed, falling back to Python: {e}")
            # Fall through to Python implementation

        # Fallback to Python implementation
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

    def _fetch_tuples_for_rust(
        self, tenant_id: str, subject: Entity | None = None
    ) -> list[dict[str, Any]]:
        """Fetch ReBAC tuples for Rust permission computation with caching.

        PERFORMANCE FIX: This method now caches tenant tuples to avoid O(T) fetches
        on every permission check. The cache is invalidated on tuple mutations.

        Cache strategy:
        - Tenant tuples: Cached with TTL (the O(T) part)
        - Cross-tenant shares: Always fresh (small, indexed query)

        Args:
            tenant_id: Tenant ID to scope tuples
            subject: Optional subject for cross-tenant share lookup

        Returns:
            List of tuple dictionaries for Rust
        """
        import logging

        logger = logging.getLogger(__name__)

        # PERFORMANCE: Check tenant tuples cache first
        cached_tuples = self._get_cached_tenant_tuples(tenant_id)

        if cached_tuples is not None:
            logger.debug(
                f"[GRAPH-CACHE] Cache HIT for tenant {tenant_id}: {len(cached_tuples)} tuples"
            )
            tuples = list(cached_tuples)  # Copy to avoid modifying cache
        else:
            # Cache miss - fetch from DB
            logger.debug(f"[GRAPH-CACHE] Cache MISS for tenant {tenant_id}, fetching from DB")
            tuples = self._fetch_tenant_tuples_from_db(tenant_id)

            # Cache the result
            self._cache_tenant_tuples(tenant_id, tuples)
            logger.debug(f"[GRAPH-CACHE] Cached {len(tuples)} tuples for tenant {tenant_id}")

        # CROSS-TENANT FIX: Always fetch cross-tenant shares fresh (small, indexed query)
        # Cross-tenant shares are stored in the resource owner's tenant but need
        # to be visible when checking permissions from the recipient's tenant.
        if subject is not None:
            cross_tenant_tuples = self._fetch_cross_tenant_shares(tenant_id, subject)
            if cross_tenant_tuples:
                logger.debug(
                    f"[GRAPH-CACHE] Fetched {len(cross_tenant_tuples)} cross-tenant shares for {subject}"
                )
                tuples.extend(cross_tenant_tuples)

        return tuples

    def _get_cached_tenant_tuples(self, tenant_id: str) -> list[dict[str, Any]] | None:
        """Get cached tenant tuples if not expired.

        Args:
            tenant_id: Tenant ID

        Returns:
            Cached tuples list or None if cache miss/expired
        """
        if tenant_id not in self._tenant_graph_cache:
            return None

        tuples, _namespace_configs, cached_at = self._tenant_graph_cache[tenant_id]
        age = time.perf_counter() - cached_at

        if age > self._tenant_graph_cache_ttl:
            # Cache expired
            del self._tenant_graph_cache[tenant_id]
            return None

        return tuples

    def _cache_tenant_tuples(self, tenant_id: str, tuples: list[dict[str, Any]]) -> None:
        """Cache tenant tuples with timestamp.

        Args:
            tenant_id: Tenant ID
            tuples: Tuples list to cache
        """
        namespace_configs = self._get_namespace_configs_for_rust()
        self._tenant_graph_cache[tenant_id] = (tuples, namespace_configs, time.perf_counter())

    def _fetch_tenant_tuples_from_db(self, tenant_id: str) -> list[dict[str, Any]]:
        """Fetch all tuples for a tenant from database.

        Args:
            tenant_id: Tenant ID

        Returns:
            List of tuple dictionaries
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id
                    FROM rebac_tuples
                    WHERE tenant_id = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                (tenant_id, datetime.now(UTC).isoformat()),
            )

            tuples = []
            for row in cursor.fetchall():
                tuples.append(
                    {
                        "subject_type": row["subject_type"],
                        "subject_id": row["subject_id"],
                        "subject_relation": row["subject_relation"],
                        "relation": row["relation"],
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                    }
                )

            return tuples

    def _fetch_cross_tenant_shares(self, tenant_id: str, subject: Entity) -> list[dict[str, Any]]:
        """Fetch cross-tenant shares for a subject.

        Cross-tenant shares are stored in the resource owner's tenant but need
        to be visible when checking permissions from the recipient's tenant.
        This query is indexed and returns only the small number of shares.

        Args:
            tenant_id: Current tenant ID (to exclude)
            subject: Subject entity to find shares for

        Returns:
            List of cross-tenant share tuples
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cross_tenant_relations = list(CROSS_TENANT_ALLOWED_RELATIONS)
            placeholders = ", ".join("?" * len(cross_tenant_relations))

            cursor.execute(
                self._fix_sql_placeholders(
                    f"""
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id
                    FROM rebac_tuples
                    WHERE relation IN ({placeholders})
                      AND subject_type = ? AND subject_id = ?
                      AND tenant_id != ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                tuple(cross_tenant_relations)
                + (
                    subject.entity_type,
                    subject.entity_id,
                    tenant_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            tuples = []
            for row in cursor.fetchall():
                tuples.append(
                    {
                        "subject_type": row["subject_type"],
                        "subject_id": row["subject_id"],
                        "subject_relation": row["subject_relation"],
                        "relation": row["relation"],
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                    }
                )

            return tuples

    def invalidate_tenant_graph_cache(self, tenant_id: str | None = None) -> None:
        """Invalidate the tenant graph cache.

        Call this when tuples are created, updated, or deleted.

        Args:
            tenant_id: Specific tenant to invalidate, or None to clear all
        """
        import logging

        logger = logging.getLogger(__name__)

        if tenant_id is None:
            count = len(self._tenant_graph_cache)
            self._tenant_graph_cache.clear()
            logger.debug(f"[GRAPH-CACHE] Cleared all {count} cached tenant graphs")
        elif tenant_id in self._tenant_graph_cache:
            del self._tenant_graph_cache[tenant_id]
            logger.debug(f"[GRAPH-CACHE] Invalidated cache for tenant {tenant_id}")

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        subject_tenant_id: str | None = None,
        object_tenant_id: str | None = None,
    ) -> str:
        """Create a relationship tuple with cache invalidation.

        Overrides parent to invalidate the tenant graph cache after writes.

        Args:
            subject: (subject_type, subject_id) or (subject_type, subject_id, subject_relation) tuple
            relation: Relation type
            object: (object_type, object_id) tuple
            expires_at: Optional expiration time
            conditions: Optional JSON conditions
            tenant_id: Tenant ID for this relationship
            subject_tenant_id: Subject's tenant
            object_tenant_id: Object's tenant

        Returns:
            Tuple ID of created relationship
        """
        # Call parent implementation
        result = super().rebac_write(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            conditions=conditions,
            tenant_id=tenant_id,
            subject_tenant_id=subject_tenant_id,
            object_tenant_id=object_tenant_id,
        )

        # Invalidate cache for affected tenants
        effective_tenant = tenant_id or "default"
        self.invalidate_tenant_graph_cache(effective_tenant)

        # For cross-tenant shares, also invalidate the other tenant
        if subject_tenant_id and subject_tenant_id != effective_tenant:
            self.invalidate_tenant_graph_cache(subject_tenant_id)
        if object_tenant_id and object_tenant_id != effective_tenant:
            self.invalidate_tenant_graph_cache(object_tenant_id)

        return result

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple with cache invalidation.

        Overrides parent to invalidate the tenant graph cache after deletes.

        Args:
            tuple_id: ID of tuple to delete

        Returns:
            True if tuple was deleted, False if not found
        """
        # First, get the tuple info to know which tenant to invalidate
        tenant_id = None
        with self._connection() as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(
                self._fix_sql_placeholders("SELECT tenant_id FROM rebac_tuples WHERE tuple_id = ?"),
                (tuple_id,),
            )
            row = cursor.fetchone()
            if row:
                tenant_id = row["tenant_id"]

        # Call parent implementation
        result = super().rebac_delete(tuple_id)

        # Invalidate cache for the affected tenant
        if result and tenant_id:
            self.invalidate_tenant_graph_cache(tenant_id)

        return result

    def _get_namespace_configs_for_rust(self) -> dict[str, Any]:
        """Get namespace configurations for Rust permission computation.

        Returns:
            Dict mapping object_type -> namespace config
        """
        # Get the standard object types that we need namespace configs for
        # These are the common object types used in permission checks
        object_types = ["file", "tenant", "user", "group", "agent", "memory"]

        configs = {}
        for obj_type in object_types:
            namespace = self.get_namespace(obj_type)
            if namespace:
                configs[obj_type] = namespace.config
        return configs

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
        memo: dict[tuple[str, str, str, str, str], bool] | None = None,
    ) -> bool:
        """Compute permission with P0-5 limits enforced at each step.

        PERF FIX: Added memo dict for cross-branch memoization.
        - visited: prevents cycles within a single path (copied per branch)
        - memo: caches results across ALL branches (shared, never copied)
        """
        import logging

        logger = logging.getLogger(__name__)
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
            memo[memo_key] = result  # Cache result
            return result

        # FIX: Check if permission is a mapped permission (e.g., "write" -> ["editor", "owner"])
        # If permission has usersets defined, check if subject has any of those relations
        if namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            if usersets:
                logger.info(
                    f"{indent}├─[PERM-MAPPING] Permission '{permission}' maps to relations: {usersets}"
                )
                # Permission is defined as a mapping to relations (e.g., write -> [editor, owner])
                # Check if subject has ANY of the relations that grant this permission
                for i, relation in enumerate(usersets):
                    logger.info(
                        f"{indent}├─[PERM-REL {i + 1}/{len(usersets)}] Checking relation '{relation}' for permission '{permission}'"
                    )
                    try:
                        result = self._compute_permission_tenant_aware_with_limits(
                            subject,
                            relation,
                            obj,
                            tenant_id,
                            visited.copy(),  # Copy visited to prevent false cycles
                            depth + 1,
                            start_time,
                            stats,
                            context,
                            memo,  # Share memo across all branches for memoization
                        )
                        logger.debug(f"{indent}│ └─[RESULT] '{relation}' = {result}")
                        if result:
                            logger.debug(f"{indent}└─[✅ GRANTED] via relation '{relation}'")
                            memo[memo_key] = True  # Cache positive result
                            return True
                    except Exception as e:
                        logger.error(
                            f"{indent}│ └─[ERROR] Exception while checking '{relation}': {type(e).__name__}: {e}"
                        )
                        raise
                logger.debug(
                    f"{indent}└─[❌ DENIED] No relations granted access for permission '{permission}'"
                )
                memo[memo_key] = False  # Cache negative result
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
            memo[memo_key] = result  # Cache result
            return result

        # Handle union (OR of multiple relations)
        if namespace.has_union(permission):
            union_relations = namespace.get_union_relations(permission)
            logger.info(f"{indent}├─[UNION] Relation '{permission}' expands to: {union_relations}")

            # P0-5: Check fan-out limit
            if self.enable_graph_limits and len(union_relations) > GraphLimits.MAX_FAN_OUT:
                raise GraphLimitExceeded("fan_out", GraphLimits.MAX_FAN_OUT, len(union_relations))

            for i, rel in enumerate(union_relations):
                logger.debug(
                    f"{indent}│ ├─[UNION {i + 1}/{len(union_relations)}] Checking: '{rel}'"
                )
                try:
                    result = self._compute_permission_tenant_aware_with_limits(
                        subject,
                        rel,
                        obj,
                        tenant_id,
                        visited.copy(),  # Copy visited to prevent false cycles
                        depth + 1,
                        start_time,
                        stats,
                        context,
                        memo,  # Share memo across all branches
                    )
                    logger.debug(f"{indent}│ │ └─[RESULT] '{rel}' = {result}")
                    if result:
                        logger.debug(f"{indent}└─[✅ GRANTED] via union member '{rel}'")
                        memo[memo_key] = True  # Cache positive result
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
            logger.debug(f"{indent}└─[❌ DENIED] - no union members granted access")
            memo[memo_key] = False  # Cache negative result
            return False

        # Handle tupleToUserset (indirect relation via another object)
        if namespace.has_tuple_to_userset(permission):
            ttu = namespace.get_tuple_to_userset(permission)
            if ttu:
                tupleset_relation = ttu["tupleset"]
                computed_userset = ttu["computedUserset"]
                logger.info(
                    f"{indent}├─[TTU] '{permission}' = tupleToUserset(tupleset='{tupleset_relation}', computed='{computed_userset}')"
                )

                # Pattern 1 (parent-style): Find objects where (obj, tupleset_relation, ?)
                # Example: (child_file, "parent", parent_dir) -> check subject has computed_userset on parent_dir
                stats.queries += 1
                if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                    raise GraphLimitExceeded(
                        "queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries
                    )

                related_objects = self._find_related_objects_tenant_aware(
                    obj, tupleset_relation, tenant_id
                )
                logger.info(
                    f"{indent}│ ├─[TTU-PARENT] Found {len(related_objects)} objects via '{tupleset_relation}': {[f'{o.entity_type}:{o.entity_id}' for o in related_objects]}"
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
                        visited.copy(),  # Copy visited to prevent false cycles
                        depth + 1,
                        start_time,
                        stats,
                        context,
                        memo,  # Share memo across all branches
                    ):
                        logger.debug(
                            f"{indent}← RESULT: True (via tupleToUserset parent pattern on {related_obj.entity_type}:{related_obj.entity_id})"
                        )
                        memo[memo_key] = True  # Cache positive result
                        return True

                # Pattern 2 (group-style): Find subjects where (?, tupleset_relation, obj)
                # Example: (group, "direct_viewer", file) -> check subject has computed_userset on group
                # IMPORTANT: Only apply Pattern 2 for group membership patterns (direct_* relations)
                # NOT for parent relations which would cause exponential blow-up checking all children
                if tupleset_relation == "parent":
                    logger.debug(
                        f"{indent}│ └─[TTU-SKIP] Skipping Pattern 2 for 'parent' tupleset (not a group pattern)"
                    )
                    memo[memo_key] = False
                    return False

                stats.queries += 1
                if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                    raise GraphLimitExceeded(
                        "queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries
                    )

                related_subjects = self._find_subjects_with_relation_tenant_aware(
                    obj, tupleset_relation, tenant_id
                )
                logger.debug(
                    f"{indent}[depth={depth}]   Pattern 2 (group): Found {len(related_subjects)} subjects with '{tupleset_relation}' on obj: {[f'{s.entity_type}:{s.entity_id}' for s in related_subjects]}"
                )

                # P0-5: Check fan-out limit for group pattern
                if self.enable_graph_limits and len(related_subjects) > GraphLimits.MAX_FAN_OUT:
                    raise GraphLimitExceeded(
                        "fan_out", GraphLimits.MAX_FAN_OUT, len(related_subjects)
                    )

                # Check if subject has computed_userset on any related subject (typically group membership)
                for related_subj in related_subjects:
                    logger.debug(
                        f"{indent}  Checking if {subject} has '{computed_userset}' on {related_subj.entity_type}:{related_subj.entity_id}"
                    )
                    if self._compute_permission_tenant_aware_with_limits(
                        subject,
                        computed_userset,
                        related_subj,
                        tenant_id,
                        visited.copy(),  # Copy visited to prevent false cycles
                        depth + 1,
                        start_time,
                        stats,
                        context,
                        memo,  # Share memo across all branches
                    ):
                        logger.debug(
                            f"{indent}← RESULT: True (via tupleToUserset group pattern on {related_subj.entity_type}:{related_subj.entity_id})"
                        )
                        memo[memo_key] = True  # Cache positive result
                        return True

            logger.debug(f"{indent}← RESULT: False (tupleToUserset found no access)")
            memo[memo_key] = False  # Cache negative result
            return False

        # Direct relation check
        logger.debug(f"{indent}[depth={depth}] Checking direct relation (fallback)")
        stats.queries += 1
        if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
            raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
        result = self._has_direct_relation_tenant_aware(
            subject, permission, obj, tenant_id, context
        )
        logger.debug(f"{indent}← [EXIT depth={depth}] Direct relation result: {result}")
        memo[memo_key] = result  # Cache result
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

        # For parent relation on files, compute from path instead of querying DB
        # This handles cross-tenant scenarios where parent tuples are in different tenant
        if relation == "parent" and obj.entity_type == "file":
            parent_path = str(PurePosixPath(obj.entity_id).parent)
            if parent_path != obj.entity_id and parent_path != ".":
                logger.debug(
                    f"_find_related_objects_tenant_aware: Computed parent from path: {obj.entity_id} -> {parent_path}"
                )
                return [Entity("file", parent_path)]
            return []

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

            logger.debug(
                f"_find_related_objects_tenant_aware: Found {len(results)} objects for {obj} via '{relation}': {[str(r) for r in results]}"
            )
            return results

    def _find_subjects_with_relation_tenant_aware(
        self, obj: Entity, relation: str, tenant_id: str
    ) -> list[Entity]:
        """Find all subjects that have a relation to obj (tenant-scoped).

        For group-style tupleToUserset traversal, finds subjects where: (subject, relation, obj)
        Example: Finding groups with direct_viewer on file X means finding tuples where:
          - subject = any (typically a group)
          - relation = "direct_viewer"
          - object = file X

        This is the reverse of _find_related_objects_tenant_aware and is used for group
        permission inheritance patterns like: group_viewer -> find groups with direct_viewer -> check member.

        Args:
            obj: Object entity (the object in the tuple)
            relation: Relation type (e.g., "direct_viewer")
            tenant_id: Tenant ID to scope the query

        Returns:
            List of subject entities (the subjects from matching tuples)
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"_find_subjects_with_relation_tenant_aware: Looking for (?, '{relation}', {obj})"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Query for tuples where obj is the OBJECT (reverse of parent pattern)
            # This handles group relations: (group, "direct_viewer", file)
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
                results.append(Entity(row["subject_type"], row["subject_id"]))

            logger.debug(
                f"_find_subjects_with_relation_tenant_aware: Found {len(results)} subjects for (?, '{relation}', {obj}): {[str(r) for r in results]}"
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
        import logging

        logger = logging.getLogger(__name__)

        # EXTENSIVE DEBUG LOGGING
        logger.info(
            f"[DIRECT-CHECK] Checking: ({subject.entity_type}:{subject.entity_id}) "
            f"has '{relation}' on ({obj.entity_type}:{obj.entity_id})? tenant={tenant_id}"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Check for direct concrete subject tuple (with ABAC conditions support)
            query = """
                    SELECT tuple_id, conditions FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND tenant_id = ?
                      AND subject_relation IS NULL
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
            params = (
                subject.entity_type,
                subject.entity_id,
                relation,
                obj.entity_type,
                obj.entity_id,
                tenant_id,
                datetime.now(UTC).isoformat(),
            )
            logger.info(f"[DIRECT-CHECK] SQL Query params: {params}")

            cursor.execute(self._fix_sql_placeholders(query), params)

            row = cursor.fetchone()
            logger.info(f"[DIRECT-CHECK] Query result row: {dict(row) if row else None}")
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

            # Cross-tenant check for shared-* relations (PR #647, #648)
            # Cross-tenant shares are stored in the resource owner's tenant
            # but should be visible when checking from the recipient's tenant.
            from nexus.core.rebac import CROSS_TENANT_ALLOWED_RELATIONS

            if relation in CROSS_TENANT_ALLOWED_RELATIONS:
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
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
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if cursor.fetchone():
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.debug(f"Cross-tenant share found: {subject} -> {relation} -> {obj}")
                    return True

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
        import time as time_module

        bulk_start = time_module.perf_counter()
        logger.debug(f"rebac_check_bulk: Checking {len(checks)} permissions in batch")

        # Log sample of checks for debugging
        if checks and len(checks) <= 10:
            logger.debug(f"[BULK-DEBUG] All checks: {checks}")
        elif checks:
            logger.debug(f"[BULK-DEBUG] First 5 checks: {checks[:5]}")
            logger.debug(f"[BULK-DEBUG] Last 5 checks: {checks[-5:]}")

        if not checks:
            return {}

        # Note: When tenant isolation is disabled, we still use bulk processing
        # but skip the tenant_id filter in the SQL query. This provides the same
        # 50-100x speedup as the tenant-isolated case. (Issue #580)

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

        # STRATEGY: Check L1 in-memory cache first (fast), then L2 DB cache, then compute
        results = {}
        cache_misses = []

        # PHASE 0: Check L1 in-memory cache first (very fast, <1ms for all checks)
        l1_start = time_module.perf_counter()
        l1_hits = 0
        l1_cache_enabled = self._l1_cache is not None
        logger.debug(
            f"[BULK-DEBUG] L1 cache enabled: {l1_cache_enabled}, consistency: {consistency}"
        )

        if (
            l1_cache_enabled
            and self._l1_cache is not None
            and consistency == ConsistencyLevel.EVENTUAL
        ):
            l1_cache_stats = self._l1_cache.get_stats()
            logger.debug(f"[BULK-DEBUG] L1 cache stats before lookup: {l1_cache_stats}")

            for check in checks:
                subject, permission, obj = check
                cached = self._l1_cache.get(
                    subject[0], subject[1], permission, obj[0], obj[1], tenant_id
                )
                if cached is not None:
                    results[check] = cached
                    l1_hits += 1
                else:
                    cache_misses.append(check)

            l1_elapsed = (time_module.perf_counter() - l1_start) * 1000
            logger.debug(
                f"[BULK-PERF] L1 cache lookup: {l1_hits} hits, {len(cache_misses)} misses in {l1_elapsed:.1f}ms"
            )

            if not cache_misses:
                total_elapsed = (time_module.perf_counter() - bulk_start) * 1000
                logger.debug(
                    f"[BULK-PERF] ✅ All {len(checks)} checks satisfied from L1 cache in {total_elapsed:.1f}ms"
                )
                return results
        else:
            cache_misses = list(checks)
            logger.debug(
                f"[BULK-DEBUG] Skipping L1 cache (enabled={l1_cache_enabled}, consistency={consistency})"
            )

        if not cache_misses:
            logger.debug("All checks satisfied from cache")
            return results

        logger.debug(f"Cache misses: {len(cache_misses)}, fetching tuples in bulk")

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

            # NEW STRATEGY: Instead of using LIKE queries (which can miss tuples and cause query explosion),
            # compute all ancestor paths for all files and fetch tuples for those specific paths.
            # This is more precise and ensures we get ALL parent tuples needed.
            ancestor_paths = set()
            for file_path in file_paths:
                # For each file, compute all ancestor paths
                # Example: /a/b/c.txt → [/a/b/c.txt, /a/b, /a, /]
                parts = file_path.strip("/").split("/")
                for i in range(len(parts), 0, -1):
                    ancestor = "/" + "/".join(parts[:i])
                    ancestor_paths.add(ancestor)
                if file_path != "/":
                    ancestor_paths.add("/")  # Always include root

            # Add all ancestor paths to BOTH subjects and objects
            # We need tuples in both directions:
            # 1. (child, "parent", ancestor) - ancestor in object position
            # 2. (ancestor, "parent", ancestor's_parent) - ancestor in subject position
            # This ensures we fetch the complete parent chain
            file_path_tuples = [("file", path) for path in ancestor_paths]
            all_objects.update(file_path_tuples)
            all_subjects.update(file_path_tuples)

            # Rebuild BOTH subject and object params to include ancestor paths
            subject_params = []
            for subj_type, subj_id in all_subjects:
                subject_params.extend([subj_type, subj_id])

            object_params = []
            for obj_type, obj_id in all_objects:
                object_params.extend([obj_type, obj_id])

            placeholders_subjects = ", ".join(["(?, ?)"] * len(all_subjects))
            placeholders_objects = ", ".join(["(?, ?)"] * len(all_objects))

            # Build full query
            # Note: We've already included all ancestor paths in all_objects above,
            # so we don't need separate file_path_conditions anymore
            where_clauses = [
                f"(subject_type, subject_id) IN ({placeholders_subjects})",
                f"(object_type, object_id) IN ({placeholders_objects})",
            ]

            # When tenant isolation is disabled, skip the tenant_id filter (Issue #580)
            if self.enforce_tenant_isolation:
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
                params = [tenant_id, datetime.now(UTC).isoformat()] + subject_params + object_params
            else:
                # No tenant_id filter when tenant isolation is disabled
                query = self._fix_sql_placeholders(
                    f"""
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE (expires_at IS NULL OR expires_at >= ?)
                      AND ({" OR ".join(where_clauses)})
                    """
                )
                params = [datetime.now(UTC).isoformat()] + subject_params + object_params
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

            # CROSS-TENANT FIX: Also fetch cross-tenant shares for subjects in the check list
            # Cross-tenant shares are stored in the resource owner's tenant but need to be
            # visible when checking permissions from the recipient's tenant.
            # This query is indexed and returns only the small number of cross-tenant tuples.
            if self.enforce_tenant_isolation:
                cross_tenant_relations = tuple(CROSS_TENANT_ALLOWED_RELATIONS)
                # Build placeholders for subject IN clause
                cross_tenant_subject_placeholders = ", ".join(["(?, ?)"] * len(all_subjects))
                cross_tenant_query = self._fix_sql_placeholders(
                    f"""
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id, conditions, expires_at
                    FROM rebac_tuples
                    WHERE relation IN ({", ".join("?" * len(cross_tenant_relations))})
                      AND (subject_type, subject_id) IN ({cross_tenant_subject_placeholders})
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                )
                cross_tenant_params = (
                    list(cross_tenant_relations) + subject_params + [datetime.now(UTC).isoformat()]
                )
                cursor.execute(cross_tenant_query, cross_tenant_params)

                cross_tenant_count = 0
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
                    cross_tenant_count += 1

                if cross_tenant_count > 0:
                    logger.debug(
                        f"Fetched {cross_tenant_count} cross-tenant share tuples for subjects"
                    )

                # PR #648: Compute parent relationships in memory (no DB query needed)
                # For files, parent relationship is deterministic from path:
                # - /workspace/project/file.txt → parent → /workspace/project
                # - /workspace/project → parent → /workspace
                # This enables cross-tenant folder sharing with children without
                # any additional DB queries or cross-tenant complexity.
                if ancestor_paths:
                    computed_parent_count = 0
                    for file_path in ancestor_paths:
                        parent_path = str(PurePosixPath(file_path).parent)
                        # Don't create self-referential parent (root's parent is root)
                        if parent_path != file_path and parent_path != ".":
                            tuples_graph.append(
                                {
                                    "subject_type": "file",
                                    "subject_id": file_path,
                                    "subject_relation": None,
                                    "relation": "parent",
                                    "object_type": "file",
                                    "object_id": parent_path,
                                    "conditions": None,
                                    "expires_at": None,
                                }
                            )
                            computed_parent_count += 1

                    if computed_parent_count > 0:
                        logger.debug(
                            f"Computed {computed_parent_count} parent tuples in memory for file hierarchy"
                        )

            logger.debug(
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

        logger.debug(
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
                logger.debug(
                    f"[SCHEMA-VERIFY] Permission '{permission}' on '{obj_type}' expands to {len(usersets)} relations: {usersets}"
                )
                logger.debug(
                    "[SCHEMA-VERIFY] Expected: 3 for hybrid schema (viewer, editor, owner) or 9 for flattened"
                )

        # TRY RUST ACCELERATION FIRST for bulk computation
        from nexus.core.rebac_fast import check_permissions_bulk_with_fallback, is_rust_available

        rust_success = False
        if is_rust_available() and len(cache_misses) >= 10:
            try:
                logger.debug(f"⚡ Attempting Rust acceleration for {len(cache_misses)} checks")

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
                    logger.debug(
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
                logger.debug(
                    f"[RUST-TIMING] {len(cache_misses)} checks in {rust_elapsed * 1000:.1f}ms = {per_check_us:.1f}µs/check"
                )

                # Convert results and cache in L1 (in-memory cache is fast)
                l1_cache_writes = 0
                for check in cache_misses:
                    subject, permission, obj = check
                    key = (subject[0], subject[1], permission, obj[0], obj[1])
                    result = rust_results_dict.get(key, False)
                    results[check] = result

                    # Write to L1 in-memory cache (fast, ~0.01ms per write)
                    if self._l1_cache is not None:
                        self._l1_cache.set(
                            subject[0], subject[1], permission, obj[0], obj[1], result, tenant_id
                        )
                        l1_cache_writes += 1

                if l1_cache_writes > 0:
                    logger.debug(
                        f"[RUST-PERF] Wrote {l1_cache_writes} results to L1 in-memory cache"
                    )

                rust_success = True
                logger.debug(f"✅ Rust acceleration successful for {len(cache_misses)} checks")

            except Exception as e:
                logger.warning(f"Rust acceleration failed: {e}, falling back to Python")
                rust_success = False

        # FALLBACK TO PYTHON if Rust not available or failed
        if not rust_success:
            logger.debug(f"🐍 Using Python for {len(cache_misses)} checks")
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

        logger.debug(f"Bulk memo cache stats: {len(bulk_memo_cache)} unique checks stored")
        logger.debug(
            f"Cache performance: {memo_stats['hits']} hits + {memo_stats['misses']} misses = {total_accesses} total accesses"
        )
        logger.debug(f"Cache hit rate: {hit_rate:.1f}% ({memo_stats['hits']}/{total_accesses})")
        logger.debug(f"Max traversal depth reached: {memo_stats.get('max_depth', 0)}")

        # Summary timing
        total_elapsed = (time_module.perf_counter() - bulk_start) * 1000
        allowed_count = sum(1 for r in results.values() if r)
        denied_count = len(results) - allowed_count
        logger.debug(
            f"[BULK-PERF] rebac_check_bulk completed: {len(results)} results "
            f"({allowed_count} allowed, {denied_count} denied) in {total_elapsed:.1f}ms"
        )

        # Log L1 cache stats after writes
        if self._l1_cache is not None:
            l1_stats_after = self._l1_cache.get_stats()
            logger.debug(f"[BULK-DEBUG] L1 cache stats after: {l1_stats_after}")

        return results

    def rebac_list_objects(
        self,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        tenant_id: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        """List objects that a subject can access with a given permission.

        This is the inverse of rebac_expand - instead of "who has permission on Y",
        it answers "what objects can subject X access".

        Optimized using Rust for performance. This is useful for:
        - File browser UI: "Show files I can access" (paginated)
        - Search results: Filter search hits by permission
        - Sharing UI: "Show files I own"
        - Audit: "What does user X have access to?"

        Performance:
        - Current filter_list approach: O(N) where N = total files
        - This method: O(M) where M = files user has access to (typically M << N)

        Args:
            subject: (subject_type, subject_id) tuple, e.g., ("user", "alice")
            permission: Permission to check (e.g., "read", "write")
            object_type: Type of objects to find (default: "file")
            tenant_id: Tenant ID for multi-tenant isolation
            path_prefix: Optional path prefix filter (e.g., "/workspace/")
            limit: Maximum number of results to return (default: 1000)
            offset: Number of results to skip for pagination (default: 0)

        Returns:
            List of (object_type, object_id) tuples that subject can access,
            sorted by object_id for consistent pagination

        Examples:
            >>> # List all files user can read
            >>> objects = manager.rebac_list_objects(
            ...     subject=("user", "alice"),
            ...     permission="read",
            ...     tenant_id="org_123",
            ... )
            >>> for obj_type, obj_id in objects:
            ...     print(f"{obj_type}: {obj_id}")

            >>> # Paginated listing with path prefix
            >>> page1 = manager.rebac_list_objects(
            ...     subject=("user", "alice"),
            ...     permission="read",
            ...     path_prefix="/workspace/",
            ...     limit=50,
            ...     offset=0,
            ... )
            >>> page2 = manager.rebac_list_objects(
            ...     subject=("user", "alice"),
            ...     permission="read",
            ...     path_prefix="/workspace/",
            ...     limit=50,
            ...     offset=50,
            ... )
        """
        import logging
        import time as time_module

        from nexus.core.rebac_fast import (
            RUST_AVAILABLE,
            list_objects_for_subject_rust,
        )

        logger = logging.getLogger(__name__)
        start_time = time_module.perf_counter()

        subject_type, subject_id = subject
        tenant_id = tenant_id or "default"

        logger.debug(
            f"[LIST-OBJECTS] Starting for {subject_type}:{subject_id} "
            f"permission={permission} object_type={object_type} "
            f"path_prefix={path_prefix} tenant_id={tenant_id}"
        )

        # Fetch all relevant tuples for this tenant
        # This includes direct relations, group memberships, etc.
        # CROSS-TENANT FIX: Include cross-tenant shares where this user is the recipient
        tuples = self._fetch_tuples_for_tenant(tenant_id, include_cross_tenant_for_user=subject_id)
        logger.debug(f"[LIST-OBJECTS] Fetched {len(tuples)} tuples for tenant {tenant_id}")

        # Get namespace configs
        namespace_configs = self._get_namespace_configs_dict()

        logger.debug(
            f"[LIST-OBJECTS] Namespace configs: file relations={len(namespace_configs.get('file', {}).get('relations', {}))} permissions={len(namespace_configs.get('file', {}).get('permissions', {}))}"
        )

        # Try Rust implementation first (much faster)
        if RUST_AVAILABLE:
            try:
                result = list_objects_for_subject_rust(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    object_type=object_type,
                    tuples=tuples,
                    namespace_configs=namespace_configs,
                    path_prefix=path_prefix,
                    limit=limit,
                    offset=offset,
                )
                elapsed = (time_module.perf_counter() - start_time) * 1000
                logger.debug(
                    f"[LIST-OBJECTS] Rust completed: {len(result)} objects in {elapsed:.1f}ms"
                )
                return result
            except Exception as e:
                logger.warning(f"Rust list_objects_for_subject failed, falling back to Python: {e}")
                # Fall through to Python implementation

        # Python fallback implementation
        return self._rebac_list_objects_python(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            object_type=object_type,
            tenant_id=tenant_id,
            tuples=tuples,
            _namespace_configs=namespace_configs,
            path_prefix=path_prefix,
            limit=limit,
            offset=offset,
        )

    def _rebac_list_objects_python(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        tenant_id: str,
        tuples: list[dict[str, Any]],
        _namespace_configs: dict[str, Any],
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        """Python fallback implementation for rebac_list_objects.

        Slower than Rust but provides same functionality when Rust is not available.
        """
        import logging
        import time as time_module

        logger = logging.getLogger(__name__)
        start_time = time_module.perf_counter()

        subject = Entity(subject_type, subject_id)

        # Build a set of candidate objects from tuples
        # Look for tuples where subject has any relation to objects of the requested type
        candidate_objects: set[tuple[str, str]] = set()

        # Get relations that might grant this permission
        permission_relations = self._get_permission_relations(permission, object_type)

        # Direct relations: subject -> relation -> object
        for t in tuples:
            if (
                t["subject_type"] == subject_type
                and t["subject_id"] == subject_id
                and t["object_type"] == object_type
                and t["relation"] in permission_relations
            ):
                candidate_objects.add((t["object_type"], t["object_id"]))

        # Group memberships: find groups subject belongs to
        groups: list[tuple[str, str]] = []
        for t in tuples:
            if (
                t["subject_type"] == subject_type
                and t["subject_id"] == subject_id
                and t["relation"] in ("member", "member-of")
            ):
                groups.append((t["object_type"], t["object_id"]))

        # Objects accessible through group membership
        for group_type, group_id in groups:
            for t in tuples:
                if (
                    t["subject_type"] == group_type
                    and t["subject_id"] == group_id
                    and t["object_type"] == object_type
                    and t["relation"] in permission_relations
                ):
                    candidate_objects.add((t["object_type"], t["object_id"]))

        # Apply path prefix filter
        if path_prefix:
            candidate_objects = {
                (obj_type, obj_id)
                for obj_type, obj_id in candidate_objects
                if obj_id.startswith(path_prefix)
            }

        # Verify each candidate with full permission check
        verified_objects: list[tuple[str, str]] = []
        for obj_type, obj_id in candidate_objects:
            obj = Entity(obj_type, obj_id)
            if self._compute_permission_bulk_helper(
                subject=subject,
                permission=permission,
                obj=obj,
                tenant_id=tenant_id,
                tuples_graph=tuples,
                depth=0,
            ):
                verified_objects.append((obj_type, obj_id))

        # Sort and paginate
        verified_objects.sort(key=lambda x: x[1])
        result = verified_objects[offset : offset + limit]

        elapsed = (time_module.perf_counter() - start_time) * 1000
        logger.debug(
            f"[LIST-OBJECTS] Python completed: {len(result)} objects "
            f"(from {len(candidate_objects)} candidates) in {elapsed:.1f}ms"
        )

        return result

    def _get_permission_relations(self, permission: str, object_type: str) -> set[str]:
        """Get all relations that can grant a permission.

        This expands the permission through the namespace config:
        1. permission -> usersets (e.g., "read" -> ["viewer", "editor", "owner"])
        2. Each userset -> its union members (e.g., "viewer" -> ["direct_viewer", ...])
        """
        relations: set[str] = set()

        # Check namespace config
        namespace = self.get_namespace(object_type)
        if not namespace:
            # Fallback for missing config
            return {permission, "direct_owner", "owner"}

        ns_config = namespace.config if hasattr(namespace, "config") else {}
        permissions_map = ns_config.get("permissions", {})
        relations_map = ns_config.get("relations", {})

        # Step 1: Get usersets that grant this permission
        # e.g., "read" -> ["viewer", "editor", "owner"]
        usersets = permissions_map.get(permission, [permission])
        if isinstance(usersets, list):
            relations.update(usersets)
        else:
            relations.add(permission)

        # Step 2: Expand each userset through unions
        # e.g., "viewer" -> ["direct_viewer", "parent_viewer", "group_viewer"]
        expanded: set[str] = set()
        to_expand = list(relations)

        while to_expand:
            rel = to_expand.pop()
            if rel in expanded:
                continue
            expanded.add(rel)

            # Check if this relation has a union
            rel_config = relations_map.get(rel)
            if isinstance(rel_config, dict) and "union" in rel_config:
                union_members = rel_config["union"]
                if isinstance(union_members, list):
                    for member in union_members:
                        if member not in expanded:
                            to_expand.append(member)

        return expanded

    def _fetch_tuples_for_tenant(
        self, tenant_id: str, include_cross_tenant_for_user: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch all ReBAC tuples for a tenant, optionally including cross-tenant shares.

        This is used by rebac_list_objects to get the full tuple graph.

        Args:
            tenant_id: The tenant ID to fetch tuples for
            include_cross_tenant_for_user: If provided, also include cross-tenant shares
                where this user is the recipient (subject). This enables users to see
                resources shared with them from other tenants.

        Returns:
            List of tuple dictionaries for graph traversal
        """
        from sqlalchemy import bindparam, text

        with self.engine.connect() as conn:
            if include_cross_tenant_for_user:
                # Include same-tenant tuples AND cross-tenant shares to this user
                # Cross-tenant shares have relation in CROSS_TENANT_ALLOWED_RELATIONS
                cross_tenant_relations = list(CROSS_TENANT_ALLOWED_RELATIONS)
                # Use bindparam with expanding=True for IN clause compatibility with SQLite
                result = conn.execute(
                    text("""
                        SELECT subject_type, subject_id, subject_relation,
                               relation, object_type, object_id
                        FROM rebac_tuples
                        WHERE (expires_at IS NULL OR expires_at > :now)
                          AND (
                              -- Same tenant tuples
                              tenant_id = :tenant_id
                              -- OR cross-tenant shares where this user is the recipient
                              OR (
                                  relation IN :cross_tenant_relations
                                  AND subject_type = 'user'
                                  AND subject_id = :user_id
                              )
                          )
                    """).bindparams(bindparam("cross_tenant_relations", expanding=True)),
                    {
                        "tenant_id": tenant_id,
                        "now": datetime.now(UTC),
                        "cross_tenant_relations": cross_tenant_relations,
                        "user_id": include_cross_tenant_for_user,
                    },
                )
            else:
                # Original behavior: only same-tenant tuples
                result = conn.execute(
                    text("""
                        SELECT subject_type, subject_id, subject_relation,
                               relation, object_type, object_id
                        FROM rebac_tuples
                        WHERE tenant_id = :tenant_id
                          AND (expires_at IS NULL OR expires_at > :now)
                    """),
                    {"tenant_id": tenant_id, "now": datetime.now(UTC)},
                )
            return [
                {
                    "subject_type": row.subject_type,
                    "subject_id": row.subject_id,
                    "subject_relation": row.subject_relation,
                    "relation": row.relation,
                    "object_type": row.object_type,
                    "object_id": row.object_id,
                }
                for row in result
            ]

    def _get_namespace_configs_dict(self) -> dict[str, Any]:
        """Get namespace configs as a dict for Rust interop."""
        configs: dict[str, Any] = {}
        for obj_type in ["file", "group", "tenant", "memory"]:
            namespace = self.get_namespace(obj_type)
            if namespace and namespace.config:
                configs[obj_type] = {
                    "relations": namespace.config.get("relations", {}),
                    "permissions": namespace.config.get("permissions", {}),
                }
        return configs

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
                    logger.debug(
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

                # Pattern 1 (parent-style): Find objects where (obj, tupleset_relation, ?)
                related_objects = self._find_related_objects_in_graph(
                    obj, tupleset_relation, tuples_graph
                )
                logger.debug(
                    f"_compute_permission_bulk_helper [depth={depth}]: Pattern 1 (parent) found {len(related_objects)} related objects via '{tupleset_relation}'"
                )

                # Check if subject has computed_userset on any related object
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
                            f"_compute_permission_bulk_helper [depth={depth}]: GRANTED via tupleToUserset parent pattern through {related_obj}"
                        )
                        if bulk_memo_cache is not None:
                            bulk_memo_cache[memo_key] = True
                        return True

                # Pattern 2 (group-style): Find subjects where (?, tupleset_relation, obj)
                related_subjects = self._find_subjects_in_graph(
                    obj, tupleset_relation, tuples_graph
                )
                logger.debug(
                    f"_compute_permission_bulk_helper [depth={depth}]: Pattern 2 (group) found {len(related_subjects)} subjects with '{tupleset_relation}' on obj"
                )

                # Check if subject has computed_userset on any related subject (typically group membership)
                for related_subj in related_subjects:
                    if self._compute_permission_bulk_helper(
                        subject,
                        computed_userset,
                        related_subj,
                        tenant_id,
                        tuples_graph,
                        depth + 1,
                        visited.copy(),
                        bulk_memo_cache,
                        memo_stats,
                    ):
                        logger.debug(
                            f"_compute_permission_bulk_helper [depth={depth}]: GRANTED via tupleToUserset group pattern through {related_subj}"
                        )
                        if bulk_memo_cache is not None:
                            bulk_memo_cache[memo_key] = True
                        return True

                logger.debug(
                    f"_compute_permission_bulk_helper [depth={depth}]: No related objects/subjects granted permission"
                )
                # Store result in memo cache before returning
                if bulk_memo_cache is not None:
                    bulk_memo_cache[memo_key] = False
                return False
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

        return related

    def _find_subjects_in_graph(
        self,
        obj: Entity,
        tupleset_relation: str,
        tuples_graph: list[dict[str, Any]],
    ) -> list[Entity]:
        """Find all subjects that have a relation to obj in the pre-fetched graph.

        This is used for group-style tupleToUserset traversal. For example:
        - To find groups with direct_viewer on file: look for tuples (group, "direct_viewer", file)

        Args:
            obj: Object that subjects have relations to
            tupleset_relation: Relation name (e.g., "direct_viewer", "direct_owner")
            tuples_graph: Pre-fetched tuples

        Returns:
            List of subject Entity objects
        """
        subjects = []
        for tuple_data in tuples_graph:
            # For group inheritance: (group, "direct_viewer", file)
            # obj is the file, we want to find groups
            if (
                tuple_data["object_type"] == obj.entity_type
                and tuple_data["object_id"] == obj.entity_id
                and tuple_data["relation"] == tupleset_relation
            ):
                # The subject of this tuple is the related entity
                subjects.append(Entity(tuple_data["subject_type"], tuple_data["subject_id"]))

        return subjects
