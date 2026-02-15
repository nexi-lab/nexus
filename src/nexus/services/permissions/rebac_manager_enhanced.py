"""
Enhanced ReBAC Manager with P0 Fixes

This module implements critical security and reliability fixes for GA:
- P0-1: Consistency levels and version tokens
- P0-2: Zone scoping (integrates ZoneAwareReBACManager)
- P0-5: Graph limits and DoS protection

Usage:
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager, ConsistencyLevel

    manager = EnhancedReBACManager(engine)

    # P0-1: Explicit consistency control
    result = manager.rebac_check(
        subject=("user", "alice"),
        permission="read",
        object=("file", "/doc.txt"),
        zone_id="org_123",
        consistency=ConsistencyLevel.STRONG,  # Bypass cache
    )

    # P0-5: Graph limits prevent DoS
    # Automatically enforces timeout, fan-out, and memory limits
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS, Entity
from nexus.services.permissions.rebac_manager_zone_aware import ZoneAwareReBACManager
from nexus.services.permissions.utils.zone import normalize_zone_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.services.permissions.leopard import LeopardIndex
    from nexus.services.permissions.rebac_iterator_cache import IteratorCache
    from nexus.services.permissions.tiger_cache import TigerCache, TigerCacheUpdater

logger = logging.getLogger(__name__)


# ============================================================================
# P0-1: Consistency Levels and Version Tokens
# ============================================================================


class ConsistencyLevel(Enum):
    """Consistency levels for permission checks.

    Controls cache behavior and staleness guarantees:
    - EVENTUAL: Use cache (up to 5min staleness), fastest
    - BOUNDED: Max 1s staleness
    - STRONG: Bypass cache, fresh read, slowest but most accurate

    Note: For new code, prefer ConsistencyMode with ConsistencyRequirement
    which aligns with SpiceDB/Zanzibar naming conventions.
    """

    EVENTUAL = "eventual"  # Use cache (5min staleness)
    BOUNDED = "bounded"  # Max 1s staleness
    STRONG = "strong"  # Bypass cache, fresh read


class ConsistencyMode(Enum):
    """Per-request consistency modes aligned with SpiceDB/Zanzibar (Issue #1081).

    Provides fine-grained control over cache behavior on a per-request basis,
    following industry best practices from Google Zanzibar and SpiceDB.

    Modes:
    - MINIMIZE_LATENCY: Use cache for fastest response (~99% of requests)
    - AT_LEAST_AS_FRESH: Cache must be >= min_revision (read-after-write)
    - FULLY_CONSISTENT: Bypass cache entirely (security audits, <1% of requests)

    See:
    - https://authzed.com/docs/spicedb/concepts/consistency
    - https://www.usenix.org/system/files/atc19-pang.pdf (Zanzibar paper)
    """

    MINIMIZE_LATENCY = "minimize_latency"  # Default: use cache, fastest
    AT_LEAST_AS_FRESH = "at_least_as_fresh"  # Cache if revision >= min_revision
    FULLY_CONSISTENT = "fully_consistent"  # Bypass cache, slowest but freshest


@dataclass(slots=True)
class ConsistencyRequirement:
    """Per-request consistency requirement (Issue #1081).

    Combines a consistency mode with optional parameters like min_revision.
    This follows the SpiceDB/Zanzibar pattern for per-request consistency control.

    Examples:
        # Default: maximize cache usage (99% of requests)
        ConsistencyRequirement()

        # Read-after-write: ensure we see a recent write
        ConsistencyRequirement(
            mode=ConsistencyMode.AT_LEAST_AS_FRESH,
            min_revision=write_result.revision
        )

        # Security audit: bypass all caches
        ConsistencyRequirement(mode=ConsistencyMode.FULLY_CONSISTENT)

    Attributes:
        mode: The consistency mode to use
        min_revision: Required for AT_LEAST_AS_FRESH - minimum acceptable revision
    """

    mode: ConsistencyMode = ConsistencyMode.MINIMIZE_LATENCY
    min_revision: int | None = None

    def __post_init__(self) -> None:
        """Validate consistency requirement."""
        if self.mode == ConsistencyMode.AT_LEAST_AS_FRESH and self.min_revision is None:
            raise ValueError(
                "min_revision is required for AT_LEAST_AS_FRESH mode. "
                "Pass the revision from a previous write operation."
            )

    def to_legacy_level(self) -> ConsistencyLevel:
        """Convert to legacy ConsistencyLevel for backward compatibility."""
        if self.mode == ConsistencyMode.FULLY_CONSISTENT:
            return ConsistencyLevel.STRONG
        elif self.mode == ConsistencyMode.AT_LEAST_AS_FRESH:
            return ConsistencyLevel.BOUNDED
        else:
            return ConsistencyLevel.EVENTUAL


@dataclass(slots=True)
class WriteResult:
    """Result of a permission write with consistency metadata (Issue #1081).

    Following the Zanzibar zookie pattern, writes return a consistency token
    that can be used for subsequent read-your-writes queries.

    Example:
        # Write a permission
        result = manager.rebac_write(subject, relation, object, zone_id=zone)

        # Immediately check with read-your-writes guarantee
        allowed = manager.rebac_check(
            subject, permission, object,
            consistency=ConsistencyRequirement(
                mode=ConsistencyMode.AT_LEAST_AS_FRESH,
                min_revision=result.revision
            )
        )

    Attributes:
        tuple_id: UUID of the created relationship tuple
        revision: The revision number after this write (for AT_LEAST_AS_FRESH)
        consistency_token: Opaque token encoding the revision (for clients)
        written_at_ms: Timestamp when write was persisted (epoch ms)
    """

    tuple_id: str
    revision: int
    consistency_token: str
    written_at_ms: float


@dataclass(slots=True)
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


@dataclass(slots=True)
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


class EnhancedReBACManager(ZoneAwareReBACManager):
    """ReBAC Manager with all P0 fixes integrated.

    Combines:
    - P0-1: Consistency levels and version tokens
    - P0-2: Zone scoping (via ZoneAwareReBACManager)
    - P0-5: Graph limits and DoS protection
    - Leopard: Pre-computed transitive group closure for O(1) group lookups

    This is the GA-ready ReBAC implementation.
    """

    # Relations that represent group membership
    MEMBERSHIP_RELATIONS = frozenset({"member-of", "member", "belongs-to"})

    def __init__(
        self,
        engine: Engine,
        cache_ttl_seconds: int = 300,
        max_depth: int = 50,
        enforce_zone_isolation: bool = True,
        enable_graph_limits: bool = True,
        enable_leopard: bool = True,
        enable_tiger_cache: bool = True,
    ):
        """Initialize enhanced ReBAC manager.

        Args:
            engine: SQLAlchemy database engine
            cache_ttl_seconds: Cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 10 hops)
            enforce_zone_isolation: Enable zone isolation checks (default: True)
            enable_graph_limits: Enable graph limit enforcement (default: True)
            enable_leopard: Enable Leopard transitive closure index (default: True)
            enable_tiger_cache: Enable Tiger Cache for materialized permissions (default: True)
        """
        super().__init__(engine, cache_ttl_seconds, max_depth, enforce_zone_isolation)
        self.enable_graph_limits = enable_graph_limits
        self.enable_leopard = enable_leopard
        self.enable_tiger_cache = enable_tiger_cache
        # REMOVED: self._version_counter (replaced with DB sequence in Issue #2 fix)

        # PERFORMANCE FIX: Cache zone tuples to avoid O(T) fetch per permission check
        # Key: zone_id, Value: (tuples_list, namespace_configs, cached_at_timestamp)
        # This dramatically reduces DB queries: from O(T) per check to O(1) amortized
        self._zone_graph_cache: dict[str, tuple[list[dict[str, Any]], dict[str, Any], float]] = {}
        self._zone_graph_cache_ttl = cache_ttl_seconds  # Reuse existing TTL

        # Leopard index for O(1) transitive group lookups (Issue #692)
        self._leopard: LeopardIndex | None = None
        if enable_leopard:
            from nexus.services.permissions.leopard import LeopardIndex

            self._leopard = LeopardIndex(
                engine=engine,
                cache_enabled=True,
                cache_max_size=100_000,
            )

        # Tiger Cache for materialized permissions (Issue #682)
        # Only enable on PostgreSQL - SQLite has lock contention issues
        self._tiger_cache: TigerCache | None = None
        self._tiger_updater: TigerCacheUpdater | None = None
        if enable_tiger_cache and engine.dialect.name == "postgresql":
            from nexus.services.permissions.tiger_cache import (
                TigerCache,
                TigerCacheUpdater,
                TigerResourceMap,
            )

            resource_map = TigerResourceMap(engine)
            self._tiger_cache = TigerCache(
                engine=engine,
                resource_map=resource_map,
                rebac_manager=self,
            )
            self._tiger_updater = TigerCacheUpdater(
                engine=engine,
                tiger_cache=self._tiger_cache,
                rebac_manager=self,
            )

        # Iterator cache for paginated list operations (Issue #722)
        from nexus.services.permissions.rebac_iterator_cache import IteratorCache

        self._iterator_cache: IteratorCache = IteratorCache(
            max_size=1000,
            ttl_seconds=cache_ttl_seconds,
        )

        # Issue #922: Permission boundary cache for O(1) inheritance checks
        # Instead of walking up O(depth) parent relations in the graph,
        # cache the nearest ancestor with an explicit permission grant.
        from nexus.services.permissions.permission_boundary_cache import PermissionBoundaryCache

        self._boundary_cache: PermissionBoundaryCache = PermissionBoundaryCache()

        # Issue #922: Boundary cache invalidation callbacks (for external caches)
        # PermissionEnforcer can register a callback to invalidate its boundary cache
        # when permission tuples are written
        self._boundary_cache_invalidators: list[
            tuple[str, Any]  # (callback_id, callback_fn)
        ] = []

        # Issue #919: Directory visibility cache invalidation callbacks
        # NexusFS can register its DirectoryVisibilityCache for automatic invalidation
        # when permission tuples are written or deleted
        self._dir_visibility_invalidators: list[
            tuple[str, Any]  # (callback_id, callback_fn)
        ] = []

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,  # Issue #773: Defaults to "default" internally
        consistency: ConsistencyLevel | ConsistencyRequirement | None = None,
    ) -> bool:
        """Check permission with explicit consistency control (P0-1, Issue #1081).

        Supports both legacy ConsistencyLevel and new ConsistencyRequirement.
        The new ConsistencyRequirement enables SpiceDB/Zanzibar-style per-request
        consistency modes including AT_LEAST_AS_FRESH for read-your-writes.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation
            zone_id: Zone ID to scope check
            consistency: Consistency control, one of:
                - None: Uses EVENTUAL/MINIMIZE_LATENCY (default, fastest)
                - ConsistencyLevel.EVENTUAL: Use cache (up to 5min staleness)
                - ConsistencyLevel.BOUNDED: Max 1s staleness
                - ConsistencyLevel.STRONG: Bypass cache entirely
                - ConsistencyRequirement(mode=MINIMIZE_LATENCY): Same as EVENTUAL
                - ConsistencyRequirement(mode=AT_LEAST_AS_FRESH, min_revision=N):
                    Use cache only if revision >= N (for read-your-writes)
                - ConsistencyRequirement(mode=FULLY_CONSISTENT): Same as STRONG

        Returns:
            True if permission is granted, False otherwise

        Raises:
            GraphLimitExceeded: If graph traversal exceeds limits (P0-5)

        Example:
            # Default: maximize cache usage
            result = manager.rebac_check(subject, permission, object)

            # After a write, ensure we see the new permission
            write_result = manager.rebac_write(subject, relation, object, ...)
            result = manager.rebac_check(
                subject, permission, object,
                consistency=ConsistencyRequirement(
                    mode=ConsistencyMode.AT_LEAST_AS_FRESH,
                    min_revision=write_result.revision
                )
            )

            # Security audit: bypass all caches
            result = manager.rebac_check(
                subject, permission, object,
                consistency=ConsistencyRequirement(mode=ConsistencyMode.FULLY_CONSISTENT)
            )
        """
        # Issue #1081: Normalize consistency parameter
        consistency_level: ConsistencyLevel
        min_revision: int | None = None

        if consistency is None:
            consistency_level = ConsistencyLevel.EVENTUAL
        elif isinstance(consistency, ConsistencyRequirement):
            # New-style ConsistencyRequirement
            consistency_level = consistency.to_legacy_level()
            min_revision = consistency.min_revision
        else:
            # Legacy ConsistencyLevel
            consistency_level = consistency
        logger.debug(
            f"EnhancedReBACManager.rebac_check called: enforce_zone_isolation={self.enforce_zone_isolation}, MAX_DEPTH={GraphLimits.MAX_DEPTH}"
        )

        object_type, object_id = object
        subject_type, subject_id = subject
        effective_zone = normalize_zone_id(zone_id)

        # OPTIMIZATION 1: Boundary Cache (Issue #922) - O(1) inheritance shortcut
        # For file permissions, check if we have a cached boundary (nearest ancestor with grant)
        if (
            object_type == "file"
            and permission in ("read", "write", "execute")
            and self._boundary_cache
        ):
            boundary = self._boundary_cache.get_boundary(
                effective_zone, subject_type, subject_id, permission, object_id
            )
            if boundary:
                # Found cached boundary - verify it's still valid by checking the boundary path
                boundary_result = self._check_direct_grant(
                    subject, permission, (object_type, boundary), zone_id
                )
                if boundary_result:
                    logger.debug(f"  -> Boundary Cache HIT: {object_id} → {boundary}")
                    return True
                else:
                    # Boundary no longer valid - invalidate
                    logger.debug(
                        f"  -> Boundary Cache STALE: {boundary} no longer grants {permission}"
                    )
                    self._boundary_cache.invalidate_permission_change(
                        effective_zone, subject_type, subject_id, permission, boundary
                    )

        # OPTIMIZATION 2: Try Tiger Cache (O(1) bitmap lookup)
        # Tiger Cache stores pre-materialized permissions as Roaring Bitmaps
        if self._tiger_cache and zone_id:
            tiger_result = self.tiger_check_access(
                subject=subject,
                permission=permission,
                object=object,
            )
            if tiger_result is True:
                logger.debug("  -> Tiger Cache HIT: ALLOW")
                return True
            elif tiger_result is False:
                # Explicit denial in cache - but still check graph for potential grants
                # (Tiger Cache may be stale, so we don't return False here)
                logger.debug("  -> Tiger Cache: explicit deny, checking graph")
            # If tiger_result is None, cache miss - continue with normal check

        # If zone isolation is disabled, use base ReBACManager implementation
        if not self.enforce_zone_isolation:
            from nexus.services.permissions.rebac_manager import ReBACManager

            logger.debug(f"  -> Falling back to base ReBACManager, base max_depth={self.max_depth}")
            result = ReBACManager.rebac_check(self, subject, permission, object, context, zone_id)

            # Write-through to Tiger Cache (Issue #935)
            if result and self._tiger_cache and zone_id:
                self._tiger_write_through_single(subject, permission, object, zone_id, logger)

            # Issue #922: Cache boundary if permission was granted via parent
            if result and object_type == "file" and self._boundary_cache:
                self._cache_boundary_if_inherited(subject, permission, object, zone_id, logger)

            return result

        logger.debug("  -> Using rebac_check_detailed")
        detailed_result = self.rebac_check_detailed(
            subject, permission, object, context, zone_id, consistency_level, min_revision
        )
        logger.debug(
            f"  -> rebac_check_detailed result: allowed={detailed_result.allowed}, indeterminate={detailed_result.indeterminate}"
        )

        # Write-through to Tiger Cache (Issue #935)
        if detailed_result.allowed and self._tiger_cache and zone_id:
            self._tiger_write_through_single(subject, permission, object, zone_id, logger)

        # Issue #922: Cache boundary if permission was granted via parent
        if detailed_result.allowed and object_type == "file" and self._boundary_cache:
            self._cache_boundary_if_inherited(subject, permission, object, zone_id, logger)

        return detailed_result.allowed

    def _check_direct_grant(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None,
    ) -> bool:
        """Check if subject has a DIRECT grant on the object (no inheritance).

        Issue #922: Used by boundary cache to verify cached boundaries are still valid.
        This is a lightweight check that only looks for direct grants, not inherited ones.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            zone_id: Zone ID

        Returns:
            True if direct grant exists, False otherwise
        """
        # Map permission to relations that grant it
        direct_relations = {
            "read": ["direct_viewer", "direct_editor", "direct_owner", "viewer", "editor", "owner"],
            "write": ["direct_editor", "direct_owner", "editor", "owner"],
            "execute": ["direct_owner", "owner"],
        }

        relations_to_check = direct_relations.get(permission, [])
        if not relations_to_check:
            return False

        # Check if any direct relation tuple exists
        for relation in relations_to_check:
            if self.has_direct_relation(subject, relation, object, zone_id):
                return True

        return False

    def has_direct_relation(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None,
    ) -> bool:
        """Check if a specific relation tuple exists (no graph traversal).

        Issue #922: Used by boundary cache to check for direct grants.

        Args:
            subject: (subject_type, subject_id) tuple
            relation: Relation name (e.g., "direct_viewer")
            object: (object_type, object_id) tuple
            zone_id: Zone ID

        Returns:
            True if tuple exists, False otherwise
        """
        from datetime import UTC, datetime

        effective_zone = normalize_zone_id(zone_id)
        subject_type, subject_id = subject
        object_type, object_id = object

        from nexus.core.rebac import WILDCARD_SUBJECT

        with self._connection() as conn:
            cursor = self._create_cursor(conn)
            # Check 1: Direct subject match (zone-scoped)
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT 1 FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND zone_id = ?
                      AND subject_relation IS NULL
                      AND (expires_at IS NULL OR expires_at >= ?)
                    LIMIT 1
                    """
                ),
                (
                    subject_type,
                    subject_id,
                    relation,
                    object_type,
                    object_id,
                    effective_zone,
                    datetime.now(UTC).isoformat(),
                ),
            )
            if cursor.fetchone() is not None:
                return True

            # Check 2: Wildcard/public access (Issue #1064)
            # Wildcards should grant access across ALL zones.
            # Only check if subject is not already the wildcard.
            if (subject_type, subject_id) != WILDCARD_SUBJECT:
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT 1 FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND subject_relation IS NULL
                          AND (expires_at IS NULL OR expires_at >= ?)
                        LIMIT 1
                        """
                    ),
                    (
                        WILDCARD_SUBJECT[0],
                        WILDCARD_SUBJECT[1],
                        relation,
                        object_type,
                        object_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if cursor.fetchone() is not None:
                    return True

            return False

    def _cache_boundary_if_inherited(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None,
        logger: Any,
    ) -> None:
        """Cache the permission boundary if permission was granted via parent inheritance.

        Issue #922: After graph traversal grants permission, check if it was via a parent.
        If so, cache the parent as the boundary for future O(1) lookups.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission that was granted
            object: (object_type, object_id) tuple
            zone_id: Zone ID
            logger: Logger instance
        """
        import os

        object_type, object_id = object
        subject_type, subject_id = subject
        effective_zone = normalize_zone_id(zone_id)

        # Check if this was a direct grant (if so, no need to cache boundary)
        if self._check_direct_grant(subject, permission, object, zone_id):
            return  # Direct grant, no boundary caching needed

        # Walk up the path to find the boundary (ancestor with direct grant)
        current_path = object_id
        while current_path and current_path != "/":
            parent_path = os.path.dirname(current_path)
            if not parent_path:
                parent_path = "/"

            # Check if parent has direct grant
            if self._check_direct_grant(subject, permission, (object_type, parent_path), zone_id):
                # Found the boundary! Cache it
                self._boundary_cache.set_boundary(
                    effective_zone, subject_type, subject_id, permission, object_id, parent_path
                )
                logger.info(
                    f"[BoundaryCache] Cached: {subject_type}:{subject_id} {permission} "
                    f"{object_id} → {parent_path}"
                )
                return

            if parent_path == "/":
                break
            current_path = parent_path

    def rebac_check_detailed(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,  # Issue #773: Defaults to "default" internally
        consistency: ConsistencyLevel = ConsistencyLevel.EVENTUAL,
        min_revision: int | None = None,  # Issue #1081: For AT_LEAST_AS_FRESH mode
    ) -> CheckResult:
        """Check permission with detailed result metadata (P0-1, Issue #1081).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation
            zone_id: Zone ID to scope check
            consistency: Consistency level (EVENTUAL, BOUNDED, STRONG)
            min_revision: Minimum acceptable revision for AT_LEAST_AS_FRESH mode.
                When provided with BOUNDED consistency, cache will only be used
                if the cached entry was created at revision >= min_revision.

        Returns:
            CheckResult with consistency metadata and traversal stats
        """
        # BUGFIX (Issue #3): Fail fast on missing zone_id in production
        # In production, missing zone_id is a security issue - reject immediately
        if not zone_id:
            import os

            # Public role checks are zone-agnostic, so skip warning
            is_public_check = subject[0] == "role" and subject[1] == "public"

            # Check if we're in production mode (via env var or config)
            is_production = (
                os.getenv("NEXUS_ENV") == "production" or os.getenv("ENVIRONMENT") == "production"
            )

            if is_production and not is_public_check:
                # SECURITY: In production, missing zone_id is a critical error
                logger.error("rebac_check called without zone_id in production - REJECTING")
                raise ValueError(
                    "zone_id is required for permission checks in production. "
                    "Missing zone_id can lead to cross-zone data leaks. "
                    "Set NEXUS_ENV=development to allow defaulting for local testing."
                )
            elif not is_public_check:
                # Development/test: Allow defaulting but log stack trace for debugging
                import traceback

                logger.warning(
                    f"rebac_check called without zone_id, defaulting to 'default'. "
                    f"This is only allowed in development. Stack:\n{''.join(traceback.format_stack()[-5:])}"
                )
            zone_id = "default"

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
                    subject_entity, permission, object_entity, zone_id, stats, context
                )
            except GraphLimitExceeded as e:
                # BUGFIX (Issue #5): Fail-closed on limit exceeded, but mark as indeterminate
                logger.error(
                    f"GraphLimitExceeded caught: limit_type={e.limit_type}, limit_value={e.limit_value}, actual_value={e.actual_value}"
                )
                result = False
                limit_error = e

            decision_time_ms = (time.perf_counter() - start_time) * 1000
            stats.duration_ms = decision_time_ms

            return CheckResult(
                allowed=result,
                consistency_token=self._get_version_token(zone_id),
                decision_time_ms=decision_time_ms,
                cached=False,
                cache_age_ms=None,
                traversal_stats=stats,
                indeterminate=limit_error is not None,
                limit_exceeded=limit_error,
            )

        elif consistency == ConsistencyLevel.BOUNDED:
            # Bounded consistency: Max 1s staleness OR revision-based (Issue #1081)
            # If min_revision is provided, use revision-based check (AT_LEAST_AS_FRESH)
            # Otherwise fall back to time-based check
            cached = None
            cached_revision = 0

            if min_revision is not None and self._l1_cache:
                # Issue #1081: AT_LEAST_AS_FRESH mode - check cache with revision constraint
                cached, cached_revision = self._l1_cache.get_with_revision_check(
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    permission,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    zone_id,
                    min_revision,
                )
            else:
                # Legacy time-based bounded check
                cached = self._get_cached_check_zone_aware_bounded(
                    subject_entity, permission, object_entity, zone_id, max_age_seconds=1
                )

            if cached is not None:
                decision_time_ms = (time.perf_counter() - start_time) * 1000
                return CheckResult(
                    allowed=cached,
                    consistency_token=self._get_version_token(zone_id),
                    decision_time_ms=decision_time_ms,
                    cached=True,
                    cache_age_ms=None,  # Within staleness bound
                    traversal_stats=None,
                )

            # Cache miss or too old/stale - compute fresh
            stats = TraversalStats()
            limit_error = None
            try:
                result = self._compute_permission_with_limits(
                    subject_entity, permission, object_entity, zone_id, stats, context
                )
            except GraphLimitExceeded as e:
                result = False
                limit_error = e

            self._cache_check_result_zone_aware(
                subject_entity, permission, object_entity, zone_id, result
            )

            decision_time_ms = (time.perf_counter() - start_time) * 1000
            stats.duration_ms = decision_time_ms

            return CheckResult(
                allowed=result,
                consistency_token=self._get_version_token(zone_id),
                decision_time_ms=decision_time_ms,
                cached=False,
                cache_age_ms=None,
                traversal_stats=stats,
                indeterminate=limit_error is not None,
                limit_exceeded=limit_error,
            )

        else:  # ConsistencyLevel.EVENTUAL (default)
            # Eventual consistency: Use cache (up to cache_ttl_seconds staleness)
            cached = self._get_cached_check_zone_aware(
                subject_entity, permission, object_entity, zone_id
            )
            if cached is not None:
                logger.debug(f"  -> CACHE HIT: returning cached result={cached}")
                decision_time_ms = (time.perf_counter() - start_time) * 1000
                return CheckResult(
                    allowed=cached,
                    consistency_token=self._get_version_token(zone_id),
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
                    subject_entity, permission, object_entity, zone_id, stats, context
                )
            except GraphLimitExceeded as e:
                result = False
                limit_error = e

            self._cache_check_result_zone_aware(
                subject_entity, permission, object_entity, zone_id, result
            )

            decision_time_ms = (time.perf_counter() - start_time) * 1000
            stats.duration_ms = decision_time_ms

            return CheckResult(
                allowed=result,
                consistency_token=self._get_version_token(zone_id),
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
        zone_id: str,
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
            zone_id: Zone ID
            stats: Traversal statistics
            context: Optional ABAC context

        Raises:
            GraphLimitExceeded: If any limit is exceeded during traversal
        """
        start_time = time.perf_counter()

        # Try Rust acceleration first (has proper memoization, prevents timeout)
        try:
            from nexus.services.permissions.rebac_fast import (
                check_permission_single_rust,
                is_rust_available,
            )

            if is_rust_available():
                # Fetch tuples and namespace configs for Rust
                # CROSS-ZONE FIX: Pass subject to include cross-zone shares
                tuples = self._fetch_tuples_for_rust(zone_id, subject=subject)
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
        result = self._compute_permission_zone_aware_with_limits(
            subject=subject,
            permission=permission,
            obj=obj,
            zone_id=zone_id,
            visited=set(),
            depth=0,
            start_time=start_time,
            stats=stats,
            context=context,
        )

        return result

    def _fetch_tuples_for_rust(
        self, zone_id: str, subject: Entity | None = None
    ) -> list[dict[str, Any]]:
        """Fetch ReBAC tuples for Rust permission computation with caching.

        PERFORMANCE FIX: This method now caches zone tuples to avoid O(T) fetches
        on every permission check. The cache is invalidated on tuple mutations.

        Cache strategy:
        - Zone tuples: Cached with TTL (the O(T) part)
        - Cross-zone shares: Always fresh (small, indexed query)

        Args:
            zone_id: Zone ID to scope tuples
            subject: Optional subject for cross-zone share lookup

        Returns:
            List of tuple dictionaries for Rust
        """

        # PERFORMANCE: Check zone tuples cache first
        cached_tuples = self._get_cached_zone_tuples(zone_id)

        if cached_tuples is not None:
            logger.debug(f"[GRAPH-CACHE] Cache HIT for zone {zone_id}: {len(cached_tuples)} tuples")
            tuples = list(cached_tuples)  # Copy to avoid modifying cache
        else:
            # Cache miss - fetch from DB
            logger.debug(f"[GRAPH-CACHE] Cache MISS for zone {zone_id}, fetching from DB")
            tuples = self._fetch_zone_tuples_from_db(zone_id)

            # Cache the result
            self._cache_zone_tuples(zone_id, tuples)
            logger.debug(f"[GRAPH-CACHE] Cached {len(tuples)} tuples for zone {zone_id}")

        # CROSS-ZONE FIX: Always fetch cross-zone shares fresh (small, indexed query)
        # Cross-zone shares are stored in the resource owner's zone but need
        # to be visible when checking permissions from the recipient's zone.
        if subject is not None:
            cross_zone_tuples = self._fetch_cross_zone_shares(zone_id, subject)
            if cross_zone_tuples:
                logger.debug(
                    f"[GRAPH-CACHE] Fetched {len(cross_zone_tuples)} cross-zone shares for {subject}"
                )
                tuples.extend(cross_zone_tuples)

        # WILDCARD FIX (Issue #1064): Fetch cross-zone wildcard tuples (*:*)
        # Wildcard tuples grant access to ALL users regardless of zone.
        # This is the industry-standard pattern used by SpiceDB, OpenFGA, and Ory Keto.
        wildcard_tuples = self._fetch_cross_zone_wildcards(zone_id)
        if wildcard_tuples:
            logger.debug(f"[GRAPH-CACHE] Fetched {len(wildcard_tuples)} cross-zone wildcard tuples")
            tuples.extend(wildcard_tuples)

        # LEOPARD OPTIMIZATION (Issue #840): Add synthetic membership tuples from
        # transitive closure. This allows O(1) group membership lookups instead of
        # O(depth) recursive graph traversal during permission checks.
        if self._leopard and subject is not None:
            transitive_groups = self._leopard.get_transitive_groups(
                member_type=subject.entity_type,
                member_id=subject.entity_id,
                zone_id=zone_id,
            )
            if transitive_groups:
                logger.debug(
                    f"[LEOPARD] Adding {len(transitive_groups)} synthetic membership tuples "
                    f"for {subject.entity_type}:{subject.entity_id}"
                )
                for group_type, group_id in transitive_groups:
                    tuples.append(
                        {
                            "subject_type": subject.entity_type,
                            "subject_id": subject.entity_id,
                            "subject_relation": None,
                            "relation": "member",  # synthetic direct membership
                            "object_type": group_type,
                            "object_id": group_id,
                        }
                    )

        return tuples

    def _get_cached_zone_tuples(self, zone_id: str) -> list[dict[str, Any]] | None:
        """Get cached zone tuples if not expired.

        Args:
            zone_id: Zone ID

        Returns:
            Cached tuples list or None if cache miss/expired
        """
        if zone_id not in self._zone_graph_cache:
            return None

        tuples, _namespace_configs, cached_at = self._zone_graph_cache[zone_id]
        age = time.perf_counter() - cached_at

        if age > self._zone_graph_cache_ttl:
            # Cache expired
            del self._zone_graph_cache[zone_id]
            return None

        return tuples

    def _cache_zone_tuples(self, zone_id: str, tuples: list[dict[str, Any]]) -> None:
        """Cache zone tuples with timestamp.

        Args:
            zone_id: Zone ID
            tuples: Tuples list to cache
        """
        namespace_configs = self._get_namespace_configs_for_rust()
        self._zone_graph_cache[zone_id] = (tuples, namespace_configs, time.perf_counter())

    def get_zone_tuples(self, zone_id: str) -> list[dict[str, Any]]:
        """Fetch all permission tuples for a zone (for export/portability).

        Returns raw tuples without graph traversal. Used by portability module
        for bulk export/import operations.

        Args:
            zone_id: Zone ID

        Returns:
            List of tuple dictionaries
        """
        return self._fetch_zone_tuples_from_db(zone_id)

    def _fetch_zone_tuples_from_db(self, zone_id: str) -> list[dict[str, Any]]:
        """Fetch all tuples for a zone from database.

        Args:
            zone_id: Zone ID

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
                    WHERE zone_id = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                (zone_id, datetime.now(UTC).isoformat()),
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

    def _fetch_cross_zone_shares(self, zone_id: str, subject: Entity) -> list[dict[str, Any]]:
        """Fetch cross-zone shares for a subject.

        Cross-zone shares are stored in the resource owner's zone but need
        to be visible when checking permissions from the recipient's zone.
        This query is indexed and returns only the small number of shares.

        Args:
            zone_id: Current zone ID (to exclude)
            subject: Subject entity to find shares for

        Returns:
            List of cross-zone share tuples
        """
        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
            placeholders = ", ".join("?" * len(cross_zone_relations))

            cursor.execute(
                self._fix_sql_placeholders(
                    f"""
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id
                    FROM rebac_tuples
                    WHERE relation IN ({placeholders})
                      AND subject_type = ? AND subject_id = ?
                      AND zone_id != ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                tuple(cross_zone_relations)
                + (
                    subject.entity_type,
                    subject.entity_id,
                    zone_id,
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

    def _fetch_cross_zone_wildcards(self, zone_id: str) -> list[dict[str, Any]]:
        """Fetch cross-zone wildcard tuples (*:*) (Issue #1064).

        Wildcard tuples grant access to ALL users regardless of zone.
        This query fetches all wildcard tuples from OTHER zones so they
        can be included in permission checks.

        This is the industry-standard pattern used by SpiceDB, OpenFGA, Ory Keto.

        Args:
            zone_id: Current zone ID (to exclude duplicates from same zone)

        Returns:
            List of wildcard tuples from other zones
        """
        from nexus.core.rebac import WILDCARD_SUBJECT

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id
                    FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND zone_id != ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    """
                ),
                (
                    WILDCARD_SUBJECT[0],
                    WILDCARD_SUBJECT[1],
                    zone_id,
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

    def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None:
        """Invalidate the zone graph cache.

        Call this when tuples are created, updated, or deleted.

        Args:
            zone_id: Specific zone to invalidate, or None to clear all
        """

        if zone_id is None:
            count = len(self._zone_graph_cache)
            self._zone_graph_cache.clear()
            logger.debug(f"[GRAPH-CACHE] Cleared all {count} cached zone graphs")
        elif zone_id in self._zone_graph_cache:
            del self._zone_graph_cache[zone_id]
            logger.debug(f"[GRAPH-CACHE] Invalidated cache for zone {zone_id}")

    # =========================================================================
    # Issue #922: Permission Boundary Cache Invalidation
    # =========================================================================

    def register_boundary_cache_invalidator(
        self,
        callback_id: str,
        callback: Any,
    ) -> None:
        """Register a callback to invalidate boundary cache on permission changes.

        This allows PermissionEnforcer to register its boundary cache for
        automatic invalidation when permission tuples are written.

        Args:
            callback_id: Unique identifier for this callback (for deregistration)
            callback: Function that takes (zone_id, subject_type, subject_id,
                      permission, object_path) and invalidates relevant cache entries

        Example:
            >>> def invalidator(zone_id, subject_type, subject_id, perm, path):
            ...     boundary_cache.invalidate_permission_change(
            ...         zone_id, subject_type, subject_id, perm, path
            ...     )
            >>> manager.register_boundary_cache_invalidator("enforcer1", invalidator)
        """
        # Avoid duplicate registrations
        for cid, _ in self._boundary_cache_invalidators:
            if cid == callback_id:
                return
        self._boundary_cache_invalidators.append((callback_id, callback))

    def unregister_boundary_cache_invalidator(self, callback_id: str) -> bool:
        """Unregister a boundary cache invalidation callback.

        Args:
            callback_id: ID of callback to remove

        Returns:
            True if callback was found and removed, False otherwise
        """
        for i, (cid, _) in enumerate(self._boundary_cache_invalidators):
            if cid == callback_id:
                self._boundary_cache_invalidators.pop(i)
                return True
        return False

    def _notify_boundary_cache_invalidators(
        self,
        zone_id: str,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
    ) -> None:
        """Notify all registered boundary cache invalidators of a permission change.

        Extracts the permission from the relation and calls each invalidator.
        """

        if not self._boundary_cache_invalidators:
            return

        # Map relation to permission(s) for invalidation
        # This maps relation names to permissions they grant
        relation_to_permissions: dict[str, list[str]] = {
            "direct_viewer": ["read"],
            "direct_editor": ["read", "write"],
            "direct_owner": ["read", "write", "execute"],
            "viewer": ["read"],
            "editor": ["read", "write"],
            "owner": ["read", "write", "execute"],
            "viewer-of": ["read"],
            "owner-of": ["read", "write", "execute"],
            "shared-viewer": ["read"],
            "shared-editor": ["read", "write"],
            "shared-owner": ["read", "write", "execute"],
            "traverser-of": ["read"],
            "reader": ["read"],
            "writer": ["read", "write"],
            "parent_viewer": ["read"],
            "parent_editor": ["read", "write"],
            "parent_owner": ["read", "write", "execute"],
            "group_viewer": ["read"],
            "group_editor": ["read", "write"],
            "group_owner": ["read", "write", "execute"],
        }

        permissions = relation_to_permissions.get(relation, [])
        if not permissions:
            return

        subject_type = subject[0]
        subject_id = subject[1]
        object_type = object[0]
        object_id = object[1]

        # Only invalidate for file objects (boundary cache is for file paths)
        if object_type != "file":
            return

        for callback_id, callback in self._boundary_cache_invalidators:
            try:
                for permission in permissions:
                    callback(zone_id, subject_type, subject_id, permission, object_id)
            except Exception as e:
                logger.warning(f"[BOUNDARY-CACHE] Invalidator {callback_id} failed: {e}")

    # =========================================================================
    # Issue #919: Directory Visibility Cache Invalidation
    # =========================================================================

    def register_dir_visibility_invalidator(
        self,
        callback_id: str,
        callback: Any,
    ) -> None:
        """Register a callback to invalidate directory visibility cache on permission changes.

        This allows NexusFS to register its DirectoryVisibilityCache for
        automatic invalidation when permission tuples are written or deleted.

        Args:
            callback_id: Unique identifier for this callback (for deregistration)
            callback: Function that takes (zone_id, object_path) and invalidates
                      relevant cache entries for that path and its ancestors

        Example:
            >>> def invalidator(zone_id, path):
            ...     dir_visibility_cache.invalidate_for_resource(path, zone_id)
            >>> manager.register_dir_visibility_invalidator("nexusfs", invalidator)
        """
        # Avoid duplicate registrations
        for cid, _ in self._dir_visibility_invalidators:
            if cid == callback_id:
                return
        self._dir_visibility_invalidators.append((callback_id, callback))

    def unregister_dir_visibility_invalidator(self, callback_id: str) -> bool:
        """Unregister a directory visibility cache invalidation callback.

        Args:
            callback_id: ID of callback to remove

        Returns:
            True if callback was found and removed, False otherwise
        """
        for i, (cid, _) in enumerate(self._dir_visibility_invalidators):
            if cid == callback_id:
                self._dir_visibility_invalidators.pop(i)
                return True
        return False

    def _notify_dir_visibility_invalidators(
        self,
        zone_id: str,
        object: tuple[str, str],
    ) -> None:
        """Notify all registered directory visibility cache invalidators.

        When a permission tuple is written or deleted, the directory visibility
        for the affected path and all its ancestors must be invalidated.

        Args:
            zone_id: Zone ID
            object: (object_type, object_id) tuple - only file objects trigger invalidation
        """

        if not self._dir_visibility_invalidators:
            return

        object_type = object[0]
        object_path = object[1]

        # Only invalidate for file objects (directory visibility is for file paths)
        if object_type != "file":
            return

        for callback_id, callback in self._dir_visibility_invalidators:
            try:
                callback(zone_id, object_path)
                logger.debug(
                    f"[DIR-VIS-CACHE] Invalidator {callback_id} called for {zone_id}:{object_path}"
                )
            except Exception as e:
                logger.warning(f"[DIR-VIS-CACHE] Invalidator {callback_id} failed: {e}")

    def rebac_write(  # type: ignore[override]  # Issue #1081: Returns WriteResult instead of str
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,  # Issue #773: Defaults to "default" internally
        subject_zone_id: str | None = None,  # Defaults to zone_id if not provided
        object_zone_id: str | None = None,  # Defaults to zone_id if not provided
    ) -> WriteResult:
        """Create a relationship tuple with cache invalidation (Issue #1081).

        Overrides parent to invalidate the zone graph cache after writes.
        Returns a WriteResult with consistency metadata for read-your-writes.

        Args:
            subject: (subject_type, subject_id) or (subject_type, subject_id, subject_relation) tuple
            relation: Relation type
            object: (object_type, object_id) tuple
            expires_at: Optional expiration time
            conditions: Optional JSON conditions
            zone_id: Zone ID for this relationship
            subject_zone_id: Subject's zone
            object_zone_id: Object's zone

        Returns:
            WriteResult with tuple_id, revision, and consistency_token.
            Use the revision with ConsistencyRequirement(mode=AT_LEAST_AS_FRESH, min_revision=...)
            for read-your-writes consistency.

        Example:
            # Write and immediately check with read-your-writes guarantee
            result = manager.rebac_write(subject, relation, object, zone_id=zone)
            allowed = manager.rebac_check(
                subject, permission, object,
                consistency=ConsistencyRequirement(
                    mode=ConsistencyMode.AT_LEAST_AS_FRESH,
                    min_revision=result.revision
                )
            )
        """
        write_start = time.perf_counter()
        # Issue #773: Default subject_zone_id and object_zone_id to zone_id
        effective_subject_zone = subject_zone_id if subject_zone_id is not None else zone_id
        effective_object_zone = object_zone_id if object_zone_id is not None else zone_id

        # Call parent implementation
        result = super().rebac_write(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            conditions=conditions,
            zone_id=zone_id,
            subject_zone_id=effective_subject_zone,
            object_zone_id=effective_object_zone,
        )

        # Invalidate cache for affected zones
        effective_zone = normalize_zone_id(zone_id)
        self.invalidate_zone_graph_cache(effective_zone)

        # For cross-zone shares, also invalidate the other zone
        if subject_zone_id and subject_zone_id != effective_zone:
            self.invalidate_zone_graph_cache(subject_zone_id)
        if object_zone_id and object_zone_id != effective_zone:
            self.invalidate_zone_graph_cache(object_zone_id)

        # Leopard: Update transitive closure for membership relations
        if self._leopard and relation in self.MEMBERSHIP_RELATIONS:
            subject_type = subject[0]
            subject_id = subject[1]
            object_type = object[0]
            object_id = object[1]

            try:
                entries = self._leopard.update_closure_on_membership_add(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    group_type=object_type,
                    group_id=object_id,
                    zone_id=effective_zone,
                )
                logger.debug(
                    f"[LEOPARD] Updated closure for {subject_type}:{subject_id} -> "
                    f"{object_type}:{object_id}: {entries} entries"
                )
            except Exception as e:
                # Log but don't fail the write - closure can be rebuilt
                logger.warning(f"[LEOPARD] Failed to update closure: {e}")

        # Tiger Cache: Write-through - persist grant immediately
        # This is the fast path (~1-5ms) vs queue processing (~20-40s)
        if self._tiger_cache:
            subject_tuple = (subject[0], subject[1])
            object_type = object[0]
            object_id = object[1]

            # Map relation to permissions granted
            # Based on namespace schema: read <- [viewer, editor, owner], write <- [editor, owner]
            # IMPORTANT: Include BOTH direct_* relations AND computed unions
            relation_to_permissions: dict[str, list[str]] = {
                # Direct relations (explicit grants in database)
                "direct_viewer": ["read"],
                "direct_editor": ["read", "write"],
                "direct_owner": ["read", "write", "execute"],
                # Computed relations (unions that expand from direct_*)
                "viewer": ["read"],
                "editor": ["read", "write"],
                "owner": ["read", "write", "execute"],
                # Legacy/alternative naming
                "viewer-of": ["read"],
                "owner-of": ["read", "write", "execute"],
                # Cross-zone shared relations
                "shared-viewer": ["read"],
                "shared-editor": ["read", "write"],
                "shared-owner": ["read", "write", "execute"],
                # Special relations
                "traverser-of": ["read"],  # Directory traversal
                "reader": ["read"],
                "writer": ["read", "write"],
                # Parent inheritance (grants same as their base)
                "parent_viewer": ["read"],
                "parent_editor": ["read", "write"],
                "parent_owner": ["read", "write", "execute"],
                # Group inheritance
                "group_viewer": ["read"],
                "group_editor": ["read", "write"],
                "group_owner": ["read", "write", "execute"],
            }

            # Get permissions for this relation
            # FIX: Default to empty list (no permissions) for unknown relations
            # This is fail-closed - unknown relations grant nothing
            permissions = relation_to_permissions.get(relation, [])

            # Persist each permission grant immediately
            for permission in permissions:
                self.tiger_persist_grant(
                    subject=subject_tuple,
                    permission=permission,
                    resource_type=object_type,
                    resource_id=object_id,
                    zone_id=effective_zone,
                )

            # Leopard-style Directory Grant Expansion
            # When permission is granted on a directory, expand to all descendants
            if object_type == "file" and permissions and self._is_directory_path(object_id):
                self._expand_directory_permission_grant(
                    subject=subject_tuple,
                    permissions=permissions,
                    directory_path=object_id,
                    zone_id=effective_zone,
                )

        # Issue #922: Notify boundary cache invalidators
        self._notify_boundary_cache_invalidators(effective_zone, subject, relation, object)

        # Issue #919: Notify directory visibility cache invalidators
        self._notify_dir_visibility_invalidators(effective_zone, object)

        # Invalidate L1 permission cache for affected subject and object
        # This ensures subsequent rebac_check_bulk calls see the new permission
        if self._l1_cache is not None:
            subject_type, subject_id = subject[0], subject[1]
            object_type, object_id = object[0], object[1]
            self._l1_cache.invalidate_subject(subject_type, subject_id, effective_zone)
            self._l1_cache.invalidate_object(object_type, object_id, effective_zone)

        # Issue #1081: Get revision for consistency token (Zanzibar zookie pattern)
        revision = self._get_zone_revision_for_grant(effective_zone)
        write_time_ms = (time.perf_counter() - write_start) * 1000

        return WriteResult(
            tuple_id=result,
            revision=revision,
            consistency_token=f"v{revision}",
            written_at_ms=write_time_ms,
        )

    def rebac_write_batch(
        self,
        tuples: list[dict[str, Any]],
    ) -> int:
        """Create multiple relationship tuples with cache invalidation (batch operation).

        Overrides parent to invalidate the zone graph cache after batch writes.

        Args:
            tuples: List of tuple dicts (same format as parent rebac_write_batch)

        Returns:
            Number of tuples created
        """
        # Call parent implementation
        created_count = super().rebac_write_batch(tuples)

        if created_count > 0:
            # Invalidate cache for all affected zones
            affected_zones: set[str] = set()
            for t in tuples:
                zone_id = normalize_zone_id(t.get("zone_id"))
                affected_zones.add(zone_id)
                # Also check cross-zone shares
                if t.get("subject_zone_id") and t.get("subject_zone_id") != zone_id:
                    affected_zones.add(t["subject_zone_id"])
                if t.get("object_zone_id") and t.get("object_zone_id") != zone_id:
                    affected_zones.add(t["object_zone_id"])

            # Invalidate cache for all affected zones
            for zone_id in affected_zones:
                self.invalidate_zone_graph_cache(zone_id)

            # Leopard: Update transitive closure for membership relations
            if self._leopard:
                for t in tuples:
                    relation = t.get("relation")
                    if relation in self.MEMBERSHIP_RELATIONS:
                        subject = t["subject"]
                        obj = t["object"]
                        zone_id = normalize_zone_id(t.get("zone_id"))

                        subject_type = subject[0]
                        subject_id = subject[1]
                        object_type = obj[0]
                        object_id = obj[1]

                        try:
                            entries = self._leopard.update_closure_on_membership_add(
                                subject_type=subject_type,
                                subject_id=subject_id,
                                group_type=object_type,
                                group_id=object_id,
                                zone_id=zone_id,
                            )
                            logger.debug(
                                f"[LEOPARD] Updated closure for {subject_type}:{subject_id} -> "
                                f"{object_type}:{object_id}: {entries} entries"
                            )
                        except Exception as e:
                            # Log but don't fail - closure can be rebuilt
                            logger.warning(f"[LEOPARD] Failed to update closure: {e}")

            # Tiger Cache: Write-through for bulk operations
            if self._tiger_cache:
                # Relation to permissions mapping
                # IMPORTANT: Include BOTH direct_* relations AND computed unions
                relation_to_permissions: dict[str, list[str]] = {
                    # Direct relations (explicit grants in database)
                    "direct_viewer": ["read"],
                    "direct_editor": ["read", "write"],
                    "direct_owner": ["read", "write", "execute"],
                    # Computed relations
                    "viewer": ["read"],
                    "editor": ["read", "write"],
                    "owner": ["read", "write", "execute"],
                    "viewer-of": ["read"],
                    "owner-of": ["read", "write", "execute"],
                    "shared-viewer": ["read"],
                    "shared-editor": ["read", "write"],
                    "shared-owner": ["read", "write", "execute"],
                    "traverser-of": ["read"],
                    "reader": ["read"],
                    "writer": ["read", "write"],
                    # Parent/group inheritance
                    "parent_viewer": ["read"],
                    "parent_editor": ["read", "write"],
                    "parent_owner": ["read", "write", "execute"],
                    "group_viewer": ["read"],
                    "group_editor": ["read", "write"],
                    "group_owner": ["read", "write", "execute"],
                }

                for t in tuples:
                    subject = t["subject"]
                    obj = t["object"]
                    relation = t.get("relation", "")
                    zone_id = normalize_zone_id(t.get("zone_id"))
                    subject_tuple = (subject[0], subject[1])
                    object_type = obj[0]
                    object_id = obj[1]

                    # Get permissions for this relation
                    # FIX: Default to empty list for unknown relations
                    permissions = relation_to_permissions.get(relation, [])

                    # Persist each permission grant immediately
                    for permission in permissions:
                        self.tiger_persist_grant(
                            subject=subject_tuple,
                            permission=permission,
                            resource_type=object_type,
                            resource_id=object_id,
                            zone_id=zone_id,
                        )

            # Issue #919: Notify directory visibility cache invalidators for all affected objects
            for t in tuples:
                obj = t["object"]
                zone_id = normalize_zone_id(t.get("zone_id"))
                self._notify_dir_visibility_invalidators(zone_id, obj)

            # Invalidate L1 permission cache for all affected subjects and objects
            if self._l1_cache is not None:
                for t in tuples:
                    subject = t["subject"]
                    obj = t["object"]
                    zone_id = normalize_zone_id(t.get("zone_id"))
                    self._l1_cache.invalidate_subject(subject[0], subject[1], zone_id)
                    self._l1_cache.invalidate_object(obj[0], obj[1], zone_id)

        return created_count

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a relationship tuple with cache invalidation.

        Overrides parent to invalidate the zone graph cache after deletes.

        Args:
            tuple_id: ID of tuple to delete

        Returns:
            True if tuple was deleted, False if not found
        """
        # First, get the tuple info to know which zone to invalidate
        # and for Leopard closure update
        tuple_info: dict[str, Any] | None = None
        with self._connection() as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(
                self._fix_sql_placeholders(
                    "SELECT zone_id, subject_type, subject_id, relation, "
                    "object_type, object_id FROM rebac_tuples WHERE tuple_id = ?"
                ),
                (tuple_id,),
            )
            row = cursor.fetchone()
            if row:
                tuple_info = {
                    "zone_id": row["zone_id"],
                    "subject_type": row["subject_type"],
                    "subject_id": row["subject_id"],
                    "relation": row["relation"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                }

        # Call parent implementation
        result = super().rebac_delete(tuple_id)

        # Invalidate cache for the affected zone
        if result and tuple_info:
            zone_id = tuple_info["zone_id"]
            if zone_id:
                self.invalidate_zone_graph_cache(zone_id)

            # Tiger Cache: Write-through revocation
            if self._tiger_cache:
                subject_type = tuple_info["subject_type"]
                subject_id = tuple_info["subject_id"]
                relation = tuple_info["relation"]
                object_type = tuple_info["object_type"]
                object_id = tuple_info["object_id"]

                if subject_type and subject_id and object_type and object_id:
                    # Map relation to permissions
                    # IMPORTANT: Include BOTH direct_* relations AND computed unions
                    relation_to_permissions: dict[str, list[str]] = {
                        # Direct relations (explicit grants in database)
                        "direct_viewer": ["read"],
                        "direct_editor": ["read", "write"],
                        "direct_owner": ["read", "write", "execute"],
                        # Computed relations
                        "viewer": ["read"],
                        "editor": ["read", "write"],
                        "owner": ["read", "write", "execute"],
                        "viewer-of": ["read"],
                        "owner-of": ["read", "write", "execute"],
                        "shared-viewer": ["read"],
                        "shared-editor": ["read", "write"],
                        "shared-owner": ["read", "write", "execute"],
                        "traverser-of": ["read"],
                        "reader": ["read"],
                        "writer": ["read", "write"],
                        # Parent/group inheritance
                        "parent_viewer": ["read"],
                        "parent_editor": ["read", "write"],
                        "parent_owner": ["read", "write", "execute"],
                        "group_viewer": ["read"],
                        "group_editor": ["read", "write"],
                        "group_owner": ["read", "write", "execute"],
                    }

                    # FIX: Default to empty list for unknown relations
                    permissions = relation_to_permissions.get(relation, [])

                    # Revoke each permission immediately
                    for permission in permissions:
                        try:
                            self.tiger_persist_revoke(
                                subject=(subject_type, subject_id),
                                permission=permission,
                                resource_type=object_type,
                                resource_id=object_id,
                                zone_id=normalize_zone_id(zone_id),
                            )
                        except Exception as e:
                            logger.debug(f"[TIGER] Revoke failed: {e}")

            # Leopard: Update transitive closure for membership relations
            if self._leopard and tuple_info["relation"] in self.MEMBERSHIP_RELATIONS:
                effective_zone = normalize_zone_id(zone_id)

                try:
                    entries = self._leopard.update_closure_on_membership_remove(
                        subject_type=tuple_info["subject_type"],
                        subject_id=tuple_info["subject_id"],
                        group_type=tuple_info["object_type"],
                        group_id=tuple_info["object_id"],
                        zone_id=effective_zone,
                    )
                    logger.debug(
                        f"[LEOPARD] Removed closure for "
                        f"{tuple_info['subject_type']}:{tuple_info['subject_id']} -> "
                        f"{tuple_info['object_type']}:{tuple_info['object_id']}: {entries} entries"
                    )
                except Exception as e:
                    # Log but don't fail the delete - closure can be rebuilt
                    logger.warning(f"[LEOPARD] Failed to update closure on delete: {e}")

            # Issue #919: Notify directory visibility cache invalidators
            object_tuple = (tuple_info["object_type"], tuple_info["object_id"])
            self._notify_dir_visibility_invalidators(normalize_zone_id(zone_id), object_tuple)

            # Invalidate L1 permission cache for affected subject and object
            if self._l1_cache is not None:
                subject_type = tuple_info["subject_type"]
                subject_id = tuple_info["subject_id"]
                object_type = tuple_info["object_type"]
                object_id = tuple_info["object_id"]
                effective_zone = normalize_zone_id(zone_id)
                self._l1_cache.invalidate_subject(subject_type, subject_id, effective_zone)
                self._l1_cache.invalidate_object(object_type, object_id, effective_zone)

        return result

    # ========================================================================
    # Leopard Index Methods (Issue #692)
    # ========================================================================

    def get_transitive_groups(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> set[tuple[str, str]]:
        """Get all groups a subject transitively belongs to using Leopard index.

        Uses pre-computed transitive closure for O(1) lookup instead of
        recursive graph traversal.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            zone_id: Zone ID

        Returns:
            Set of (group_type, group_id) tuples representing all groups
            the subject belongs to, directly or transitively.
        """
        if not self._leopard:
            # Fallback: compute on-the-fly (slower)
            return self._compute_transitive_groups_fallback(subject_type, subject_id, zone_id)

        return self._leopard.get_transitive_groups(
            member_type=subject_type,
            member_id=subject_id,
            zone_id=zone_id,
        )

    def _compute_transitive_groups_fallback(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> set[tuple[str, str]]:
        """Compute transitive groups without Leopard index (fallback).

        Uses BFS traversal of membership relations.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            zone_id: Zone ID

        Returns:
            Set of (group_type, group_id) tuples
        """
        from sqlalchemy import text

        groups: set[tuple[str, str]] = set()
        visited: set[tuple[str, str]] = set()
        queue: list[tuple[str, str]] = [(subject_type, subject_id)]

        # Determine SQL NOW function based on database type
        is_postgresql = "postgresql" in str(self.engine.url)
        now_sql = "NOW()" if is_postgresql else "datetime('now')"

        with self.engine.connect() as conn:
            while queue:
                curr_type, curr_id = queue.pop(0)
                if (curr_type, curr_id) in visited:
                    continue
                visited.add((curr_type, curr_id))

                # Find direct memberships
                query = text(f"""
                    SELECT object_type, object_id
                    FROM rebac_tuples
                    WHERE subject_type = :subj_type
                      AND subject_id = :subj_id
                      AND relation IN ('member-of', 'member', 'belongs-to')
                      AND zone_id = :zone_id
                      AND (expires_at IS NULL OR expires_at > {now_sql})
                """)
                result = conn.execute(
                    query,
                    {"subj_type": curr_type, "subj_id": curr_id, "zone_id": zone_id},
                )

                for row in result:
                    group = (row.object_type, row.object_id)
                    if group not in groups:
                        groups.add(group)
                        queue.append(group)

        return groups

    def rebuild_leopard_closure(self, zone_id: str) -> int:
        """Rebuild the Leopard transitive closure for a zone.

        Useful for:
        - Initial migration from existing data
        - Recovering from inconsistency
        - Periodic verification

        Args:
            zone_id: Zone ID

        Returns:
            Number of closure entries created
        """
        if not self._leopard:
            raise RuntimeError("Leopard index is not enabled")

        return self._leopard.rebuild_closure_for_zone(zone_id)

    def invalidate_leopard_cache(self, zone_id: str | None = None) -> None:
        """Invalidate Leopard in-memory cache.

        Args:
            zone_id: If provided, only invalidate for this zone.
                       If None, invalidate all.
        """
        if not self._leopard:
            return

        if zone_id:
            self._leopard.invalidate_cache_for_zone(zone_id)
        else:
            self._leopard.clear_cache()

    # ========================================================================
    # Tiger Cache Methods (Issue #682)
    # ========================================================================

    def _tiger_write_through_single(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str,
        logger: Any = None,
    ) -> None:
        """Write-through single permission result to Tiger Cache (Issue #935).

        Called after a single permission check computes a positive result.
        This is the READ path - must be non-blocking to keep reads fast.

        Strategy:
        1. Check if resource int_id is already in memory cache (no DB)
        2. If yes: update in-memory bitmap (~microseconds)
        3. If no: skip (permission grant will populate via write-through)

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission that was granted
            object: (object_type, object_id) tuple
            zone_id: Zone ID
            logger: Optional logger instance
        """
        if not self._tiger_cache:
            return

        try:
            # Check memory cache ONLY - no DB hit on read path
            # Note: resource_key excludes zone - paths are globally unique
            resource_key = (object[0], object[1])
            resource_int_id = self._tiger_cache._resource_map._uuid_to_int.get(resource_key)

            if resource_int_id is not None:
                # Fast path: resource already mapped, just update in-memory bitmap
                self._tiger_cache.add_to_bitmap(
                    subject_type=subject[0],
                    subject_id=subject[1],
                    permission=permission,
                    resource_type=object[0],
                    zone_id=zone_id,
                    resource_int_id=resource_int_id,
                )
                if logger:
                    logger.debug(
                        f"[TIGER] Read write-through: {subject[0]}:{subject[1]} "
                        f"{permission} {object[0]}:{object[1]} (int_id={resource_int_id})"
                    )
            else:
                # Resource not in memory cache - skip
                # The permission grant (write path) will populate via persist_single_grant
                if logger:
                    logger.debug(f"[TIGER] Read skip: resource {object[1]} not in memory cache")
        except Exception as e:
            # Don't fail the permission check if Tiger write fails
            if logger:
                logger.debug(f"[TIGER] Write-through failed: {e}")

    def tiger_check_access(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        _zone_id: str = "",  # Deprecated: kept for API compatibility, ignored
    ) -> bool | None:
        """Check permission using Tiger Cache (O(1) bitmap lookup).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            zone_id: Zone ID

        Returns:
            True if allowed, False if denied, None if not in cache (use rebac_check fallback)
        """
        if not self._tiger_cache:
            return None

        return self._tiger_cache.check_access(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=object[0],
            resource_id=object[1],
        )

    def tiger_get_accessible_resources(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> set[int]:
        """Get all resources accessible by subject using Tiger Cache.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission type
            resource_type: Type of resource (e.g., "file")
            zone_id: Zone ID

        Returns:
            Set of integer resource IDs (use tiger_resource_map for UUID lookup)
        """
        if not self._tiger_cache:
            return set()

        return self._tiger_cache.get_accessible_resources(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            zone_id=zone_id,
        )

    def tiger_queue_update(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        zone_id: str,
        priority: int = 100,
    ) -> int | None:
        """Queue a Tiger Cache update for background processing.

        Call this when permissions change to schedule cache rebuild.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to recompute
            resource_type: Type of resource
            zone_id: Zone ID
            priority: Priority (lower = higher priority)

        Returns:
            Queue entry ID, or None if Tiger Cache is disabled
        """
        if not self._tiger_updater:
            return None

        return self._tiger_updater.queue_update(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            zone_id=zone_id,
            priority=priority,
        )

    def tiger_persist_grant(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> bool:
        """Write-through: Persist a single permission grant to Tiger Cache.

        This is the fast path (~1-5ms) that updates both in-memory cache and
        database immediately when a permission is granted. Much faster than
        queue processing (~20-40 seconds) which recomputes all resources.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of the resource being granted
            zone_id: Zone ID

        Returns:
            True if persisted successfully, False on error
        """
        if not self._tiger_cache:
            return False

        return self._tiger_cache.persist_single_grant(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            resource_id=resource_id,
            zone_id=zone_id,
        )

    def tiger_persist_revoke(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> bool:
        """Write-through: Persist a single permission revocation to Tiger Cache.

        Critical for security - permission revocations must propagate immediately.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of the resource being revoked
            zone_id: Zone ID

        Returns:
            True if persisted successfully, False on error
        """
        if not self._tiger_cache:
            return False

        return self._tiger_cache.persist_single_revoke(
            subject_type=subject[0],
            subject_id=subject[1],
            permission=permission,
            resource_type=resource_type,
            resource_id=resource_id,
            zone_id=zone_id,
        )

    def tiger_process_queue(self, batch_size: int = 100) -> int:
        """Process pending Tiger Cache update queue.

        Call this periodically from a background worker.

        Args:
            batch_size: Maximum entries to process

        Returns:
            Number of entries processed
        """

        if not self._tiger_updater:
            logger.warning("[TIGER] tiger_process_queue: _tiger_updater is None")
            return 0

        logger.info(f"[TIGER] tiger_process_queue: calling updater (batch={batch_size})")
        result = self._tiger_updater.process_queue(batch_size=batch_size)
        logger.info(f"[TIGER] tiger_process_queue: result={result}")
        return result

    def tiger_invalidate_cache(
        self,
        subject: tuple[str, str] | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate Tiger Cache entries.

        Args:
            subject: (subject_type, subject_id) tuple (None = all subjects)
            permission: Filter by permission (None = all)
            resource_type: Filter by resource type (None = all)
            zone_id: Filter by zone (None = all)

        Returns:
            Number of entries invalidated
        """
        if not self._tiger_cache:
            return 0

        subject_type = subject[0] if subject else None
        subject_id = subject[1] if subject else None

        return self._tiger_cache.invalidate(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type=resource_type,
            zone_id=zone_id,
        )

    def tiger_register_resource(
        self,
        resource_type: str,
        resource_id: str,
        _zone_id: str = "",  # Deprecated: kept for API compatibility, ignored
    ) -> int:
        """Register a resource in the Tiger resource map.

        Call this when creating new resources to get their integer ID.

        Args:
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of resource (e.g., UUID or path)
            zone_id: Zone ID

        Returns:
            Integer ID for use in bitmaps
        """
        if not self._tiger_cache:
            return -1

        return self._tiger_cache._resource_map.get_or_create_int_id(
            resource_type=resource_type,
            resource_id=resource_id,
        )

    # =========================================================================
    # Leopard-style Directory Permission Pre-materialization
    # =========================================================================

    # Write amplification limit: max files to expand synchronously
    # Beyond this, expansion is queued for async processing
    DIRECTORY_EXPANSION_LIMIT = 10_000

    def _is_directory_path(self, path: str) -> bool:
        """Check if a path represents a directory.

        Uses heuristics since NexusFS uses implicit directories:
        1. Path ends with /
        2. Path has no file extension in the last component
        3. Files exist under this path (queried from metadata store if available)

        Args:
            path: File path to check

        Returns:
            True if path appears to be a directory
        """

        # Explicit directory marker
        if path.endswith("/"):
            return True

        # Root is always a directory
        if path == "/":
            return True

        # Check for common file extensions (not a directory)
        last_component = path.rsplit("/", 1)[-1]
        if "." in last_component:
            extension = last_component.rsplit(".", 1)[-1].lower()
            # Common file extensions that indicate NOT a directory
            file_extensions = {
                "txt",
                "md",
                "json",
                "yaml",
                "yml",
                "xml",
                "csv",
                "tsv",
                "py",
                "js",
                "ts",
                "jsx",
                "tsx",
                "html",
                "css",
                "scss",
                "java",
                "c",
                "cpp",
                "h",
                "hpp",
                "go",
                "rs",
                "rb",
                "php",
                "sql",
                "sh",
                "bash",
                "zsh",
                "ps1",
                "bat",
                "cmd",
                "png",
                "jpg",
                "jpeg",
                "gif",
                "svg",
                "ico",
                "webp",
                "pdf",
                "doc",
                "docx",
                "xls",
                "xlsx",
                "ppt",
                "pptx",
                "zip",
                "tar",
                "gz",
                "bz2",
                "7z",
                "rar",
                "mp3",
                "mp4",
                "wav",
                "avi",
                "mov",
                "mkv",
                "log",
                "ini",
                "conf",
                "cfg",
                "env",
                "lock",
            }
            if extension in file_extensions:
                return False

        # If we have a metadata store reference, check for children
        # This is the most accurate but requires a DB query
        if hasattr(self, "_metadata_store") and self._metadata_store:
            try:
                # Check if any files exist under this path
                return bool(self._metadata_store.is_implicit_directory(path))
            except Exception as e:
                logger.debug(f"[LEOPARD] Failed to check directory via metadata: {e}")

        # Default: treat paths without extensions as potential directories
        # The expansion will be a no-op if there are no descendants
        return "." not in last_component

    def _expand_directory_permission_grant(
        self,
        subject: tuple[str, str],
        permissions: list[str],
        directory_path: str,
        zone_id: str,
    ) -> None:
        """Expand a directory permission grant to all descendants (Leopard-style).

        This is the core of pre-materialization. When a permission is granted
        on a directory, expand it to ALL descendant files so that permission
        checks become O(1) bitmap lookups instead of O(depth) tree walks.

        Args:
            subject: (subject_type, subject_id) tuple
            permissions: List of permissions granted (e.g., ["read", "write"])
            directory_path: Directory path that was granted
            zone_id: Zone ID

        Trade-offs (Zanzibar Leopard pattern):
            - Write amplification: 1 grant -> N bitmap updates
            - Read optimization: O(depth) -> O(1) per file
            - Storage: O(grants) -> O(grants * avg_descendants)
        """

        if not self._tiger_cache:
            return

        # Normalize directory path
        if not directory_path.endswith("/"):
            directory_path = directory_path + "/"

        # Get current revision for consistency (prevents "new enemy" problem)
        grant_revision = self._get_zone_revision_for_grant(zone_id)

        # Get all descendants of the directory
        descendants = self._get_directory_descendants(directory_path, zone_id)

        logger.info(
            f"[LEOPARD] Directory grant expansion: {directory_path} "
            f"-> {len(descendants)} descendants for {subject[0]}:{subject[1]}"
        )

        if not descendants:
            # No descendants - just record the grant for future file integration
            for permission in permissions:
                self._tiger_cache.record_directory_grant(
                    subject_type=subject[0],
                    subject_id=subject[1],
                    permission=permission,
                    directory_path=directory_path,
                    zone_id=zone_id,
                    grant_revision=grant_revision,
                    include_future_files=True,
                )
                # Mark as completed immediately (empty directory)
                self._tiger_cache._update_grant_status(
                    subject[0],
                    subject[1],
                    permission,
                    directory_path,
                    zone_id,
                    status="completed",
                    expanded_count=0,
                    total_count=0,
                )
            return

        # Check write amplification limit
        if len(descendants) > self.DIRECTORY_EXPANSION_LIMIT:
            logger.warning(
                f"[LEOPARD] Directory {directory_path} has {len(descendants)} files, "
                f"exceeds limit {self.DIRECTORY_EXPANSION_LIMIT}. Using async expansion."
            )
            # Queue for async expansion
            for permission in permissions:
                self._tiger_cache.record_directory_grant(
                    subject_type=subject[0],
                    subject_id=subject[1],
                    permission=permission,
                    directory_path=directory_path,
                    zone_id=zone_id,
                    grant_revision=grant_revision,
                    include_future_files=True,
                )
                # Status remains "pending" - background worker will process
            return

        # Synchronous expansion for small directories
        for permission in permissions:
            # Record the directory grant first
            self._tiger_cache.record_directory_grant(
                subject_type=subject[0],
                subject_id=subject[1],
                permission=permission,
                directory_path=directory_path,
                zone_id=zone_id,
                grant_revision=grant_revision,
                include_future_files=True,
            )

            # Expand to all descendants
            expanded, completed = self._tiger_cache.expand_directory_grant(
                subject_type=subject[0],
                subject_id=subject[1],
                permission=permission,
                directory_path=directory_path,
                zone_id=zone_id,
                grant_revision=grant_revision,
                descendants=descendants,
            )

            if completed:
                logger.info(
                    f"[LEOPARD] Expanded {permission} on {directory_path}: "
                    f"{expanded} files for {subject[0]}:{subject[1]}"
                )
            else:
                logger.error(f"[LEOPARD] Failed to expand {permission} on {directory_path}")

    def _get_zone_revision_for_grant(self, zone_id: str) -> int:
        """Get current zone revision for consistency during expansion.

        This prevents the "new enemy" problem: files created after the grant
        revision are not automatically included (user must explicitly include
        future files or re-grant).

        Args:
            zone_id: Zone ID

        Returns:
            Current revision number
        """
        from sqlalchemy import text

        try:
            query = text("""
                SELECT current_version FROM rebac_version_sequences
                WHERE zone_id = :zone_id
            """)
            with self.engine.connect() as conn:
                result = conn.execute(query, {"zone_id": zone_id})
                row = result.fetchone()
                return int(row.current_version) if row else 0
        except Exception:
            return 0

    def _get_directory_descendants(
        self,
        directory_path: str,
        zone_id: str,
    ) -> list[str]:
        """Get all file paths under a directory.

        Args:
            directory_path: Directory path (with trailing /)
            zone_id: Zone ID

        Returns:
            List of descendant file paths
        """

        # Try using metadata store if available
        if hasattr(self, "_metadata_store") and self._metadata_store:
            try:
                files = self._metadata_store.list(
                    prefix=directory_path,
                    recursive=True,
                    zone_id=zone_id,
                )
                return [f.path for f in files]
            except Exception as e:
                logger.warning(f"[LEOPARD] Metadata store query failed: {e}")

        # Fallback: query file_paths table directly
        from sqlalchemy import text

        try:
            query = text("""
                SELECT virtual_path
                FROM file_paths
                WHERE virtual_path LIKE :prefix
                  AND deleted_at IS NULL
                  AND (zone_id = :zone_id OR zone_id = 'default' OR zone_id IS NULL)
            """)

            with self.engine.connect() as conn:
                result = conn.execute(query, {"prefix": f"{directory_path}%", "zone_id": zone_id})
                return [row.virtual_path for row in result]
        except Exception as e:
            logger.error(f"[LEOPARD] Failed to query descendants: {e}")
            return []

    def set_metadata_store(self, metadata_store: Any) -> None:
        """Set the metadata store reference for directory queries.

        Args:
            metadata_store: FileMetadataProtocol instance
        """
        self._metadata_store = metadata_store

    def _get_namespace_configs_for_rust(self) -> dict[str, Any]:
        """Get namespace configurations for Rust permission computation.

        Returns:
            Dict mapping object_type -> namespace config
        """
        # Get the standard object types that we need namespace configs for
        # These are the common object types used in permission checks
        object_types = ["file", "zone", "user", "group", "agent", "memory"]

        configs = {}
        for obj_type in object_types:
            namespace = self.get_namespace(obj_type)
            if namespace:
                configs[obj_type] = namespace.config
        return configs

    def _compute_permission_zone_aware_with_limits(
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
        namespace = self.get_namespace(obj.entity_type)
        if not namespace:
            logger.debug(f"{indent}  No namespace for {obj.entity_type}, checking direct relation")
            stats.queries += 1
            if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                raise GraphLimitExceeded("queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries)
            result = self._has_direct_relation_zone_aware(
                subject, permission, obj, zone_id, context
            )
            logger.debug(f"{indent}← RESULT: {result}")
            memo[memo_key] = result  # Cache result
            return result

        # FIX: Check if permission is a mapped permission (e.g., "write" -> ["editor", "owner"])
        # If permission has usersets defined, check if subject has any of those relations
        if namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            if usersets:
                logger.debug(
                    f"{indent}├─[PERM-MAPPING] Permission '{permission}' maps to relations: {usersets}"
                )
                # Permission is defined as a mapping to relations (e.g., write -> [editor, owner])
                # Check if subject has ANY of the relations that grant this permission
                for i, relation in enumerate(usersets):
                    logger.debug(
                        f"{indent}├─[PERM-REL {i + 1}/{len(usersets)}] Checking relation '{relation}' for permission '{permission}'"
                    )
                    try:
                        result = self._compute_permission_zone_aware_with_limits(
                            subject,
                            relation,
                            obj,
                            zone_id,
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
            result = self._has_direct_relation_zone_aware(
                subject, permission, obj, zone_id, context
            )
            logger.debug(f"{indent}← RESULT: {result}")
            memo[memo_key] = result  # Cache result
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
                    result = self._compute_permission_zone_aware_with_limits(
                        subject,
                        rel,
                        obj,
                        zone_id,
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
                logger.debug(
                    f"{indent}├─[TTU] '{permission}' = tupleToUserset(tupleset='{tupleset_relation}', computed='{computed_userset}')"
                )

                # Pattern 1 (parent-style): Find objects where (obj, tupleset_relation, ?)
                # Example: (child_file, "parent", parent_dir) -> check subject has computed_userset on parent_dir
                stats.queries += 1
                if self.enable_graph_limits and stats.queries > GraphLimits.MAX_TUPLE_QUERIES:
                    raise GraphLimitExceeded(
                        "queries", GraphLimits.MAX_TUPLE_QUERIES, stats.queries
                    )

                related_objects = self._find_related_objects_zone_aware(
                    obj, tupleset_relation, zone_id
                )
                logger.debug(
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
                    if self._compute_permission_zone_aware_with_limits(
                        subject,
                        computed_userset,
                        related_obj,
                        zone_id,
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

                related_subjects = self._find_subjects_with_relation_zone_aware(
                    obj, tupleset_relation, zone_id
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
                    if self._compute_permission_zone_aware_with_limits(
                        subject,
                        computed_userset,
                        related_subj,
                        zone_id,
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
        result = self._has_direct_relation_zone_aware(subject, permission, obj, zone_id, context)
        logger.debug(f"{indent}← [EXIT depth={depth}] Direct relation result: {result}")
        memo[memo_key] = result  # Cache result
        return result

    def _find_related_objects_zone_aware(
        self, obj: Entity, relation: str, zone_id: str
    ) -> list[Entity]:
        """Find all objects related to obj via relation (zone-scoped).

        Args:
            obj: Object entity
            relation: Relation type
            zone_id: Zone ID to scope the query

        Returns:
            List of related object entities within the zone
        """
        logger.debug(
            f"_find_related_objects_zone_aware: obj={obj}, relation={relation}, zone_id={zone_id}"
        )

        # For parent relation on files, compute from path instead of querying DB
        # This handles cross-zone scenarios where parent tuples are in different zone
        if relation == "parent" and obj.entity_type == "file":
            parent_path = str(PurePosixPath(obj.entity_id).parent)
            if parent_path != obj.entity_id and parent_path != ".":
                logger.debug(
                    f"_find_related_objects_zone_aware: Computed parent from path: {obj.entity_id} -> {parent_path}"
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
                      AND zone_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    obj.entity_type,
                    obj.entity_id,
                    relation,
                    zone_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                results.append(Entity(row["object_type"], row["object_id"]))

            logger.debug(
                f"_find_related_objects_zone_aware: Found {len(results)} objects for {obj} via '{relation}': {[str(r) for r in results]}"
            )
            return results

    def _find_subjects_with_relation_zone_aware(
        self, obj: Entity, relation: str, zone_id: str
    ) -> list[Entity]:
        """Find all subjects that have a relation to obj (zone-scoped).

        For group-style tupleToUserset traversal, finds subjects where: (subject, relation, obj)
        Example: Finding groups with direct_viewer on file X means finding tuples where:
          - subject = any (typically a group)
          - relation = "direct_viewer"
          - object = file X

        This is the reverse of _find_related_objects_zone_aware and is used for group
        permission inheritance patterns like: group_viewer -> find groups with direct_viewer -> check member.

        Args:
            obj: Object entity (the object in the tuple)
            relation: Relation type (e.g., "direct_viewer")
            zone_id: Zone ID to scope the query

        Returns:
            List of subject entities (the subjects from matching tuples)
        """
        logger.debug(
            f"_find_subjects_with_relation_zone_aware: Looking for (?, '{relation}', {obj})"
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
                      AND zone_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    obj.entity_type,
                    obj.entity_id,
                    relation,
                    zone_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

            results = []
            for row in cursor.fetchall():
                results.append(Entity(row["subject_type"], row["subject_id"]))

            logger.debug(
                f"_find_subjects_with_relation_zone_aware: Found {len(results)} subjects for (?, '{relation}', {obj}): {[str(r) for r in results]}"
            )
            return results

    def _has_direct_relation_zone_aware(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check if subject has direct relation to object (zone-scoped).

        Args:
            subject: Subject entity
            relation: Relation type
            obj: Object entity
            zone_id: Zone ID to scope the query
            context: Optional ABAC context for condition evaluation

        Returns:
            True if direct relation exists within the zone
        """

        # EXTENSIVE DEBUG LOGGING
        logger.debug(
            f"[DIRECT-CHECK] Checking: ({subject.entity_type}:{subject.entity_id}) "
            f"has '{relation}' on ({obj.entity_type}:{obj.entity_id})? zone={zone_id}"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Check for direct concrete subject tuple (with ABAC conditions support)
            query = """
                    SELECT tuple_id, conditions FROM rebac_tuples
                    WHERE subject_type = ? AND subject_id = ?
                      AND relation = ?
                      AND object_type = ? AND object_id = ?
                      AND zone_id = ?
                      AND subject_relation IS NULL
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
            params = (
                subject.entity_type,
                subject.entity_id,
                relation,
                obj.entity_type,
                obj.entity_id,
                zone_id,
                datetime.now(UTC).isoformat(),
            )
            logger.debug(f"[DIRECT-CHECK] SQL Query params: {params}")

            cursor.execute(self._fix_sql_placeholders(query), params)

            row = cursor.fetchone()
            logger.debug(f"[DIRECT-CHECK] Query result row: {dict(row) if row else None}")
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

            # Cross-zone check for shared-* relations (PR #647, #648)
            # Cross-zone shares are stored in the resource owner's zone
            # but should be visible when checking from the recipient's zone.
            from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS

            if relation in CROSS_ZONE_ALLOWED_RELATIONS:
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
                    logger.debug(f"Cross-zone share found: {subject} -> {relation} -> {obj}")
                    return True

            # Check for wildcard/public access (*:*) - Issue #1064
            # Wildcards grant access to ALL users regardless of zone.
            # Only check if subject is not already the wildcard.
            from nexus.core.rebac import WILDCARD_SUBJECT

            if (subject.entity_type, subject.entity_id) != WILDCARD_SUBJECT:
                # Check for wildcard tuples in ANY zone (cross-zone public access)
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        SELECT tuple_id FROM rebac_tuples
                        WHERE subject_type = ? AND subject_id = ?
                          AND relation = ?
                          AND object_type = ? AND object_id = ?
                          AND subject_relation IS NULL
                          AND (expires_at IS NULL OR expires_at >= ?)
                        LIMIT 1
                        """
                    ),
                    (
                        WILDCARD_SUBJECT[0],
                        WILDCARD_SUBJECT[1],
                        relation,
                        obj.entity_type,
                        obj.entity_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if cursor.fetchone():
                    logger.debug(
                        f"[DIRECT-CHECK] Wildcard access found: *:* -> {relation} -> {obj}"
                    )
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
                      AND zone_id = ?
                      AND (expires_at IS NULL OR expires_at >= ?)
                    """
                ),
                (
                    relation,
                    obj.entity_type,
                    obj.entity_id,
                    zone_id,
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
                from nexus.services.permissions.rebac_manager_enhanced import TraversalStats

                sub_stats = TraversalStats()
                userset_entity = Entity(userset_type, userset_id)

                # Use a bounded sub-check to prevent infinite recursion
                # We inherit the same visited set to detect cycles across the full graph
                try:
                    if self._compute_permission_zone_aware_with_limits(
                        subject=subject,
                        permission=userset_relation,
                        obj=userset_entity,
                        zone_id=zone_id,
                        visited=set(),  # Fresh visited set for this sub-check
                        depth=0,  # Reset depth for sub-check
                        start_time=time.perf_counter(),  # Fresh timer
                        stats=sub_stats,
                        context=context,
                    ):
                        return True
                except GraphLimitExceeded:
                    # If userset check hits limits, skip this userset and try others
                    logger.warning(
                        f"Userset check hit limits: {subject} -> {userset_relation} -> {userset_entity}, skipping"
                    )
                    continue

            return False

    def _get_version_token(self, zone_id: str = "default") -> str:
        """Get current version token (P0-1).

        BUGFIX (Issue #2): Use DB-backed per-zone sequence instead of in-memory counter.
        This ensures version tokens are:
        - Monotonic across process restarts
        - Consistent across multiple processes/replicas
        - Scoped per-zone for proper isolation

        Args:
            zone_id: Zone ID to get version for

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
                    INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                    VALUES (%s, 1, NOW())
                    ON CONFLICT (zone_id)
                    DO UPDATE SET current_version = rebac_version_sequences.current_version + 1,
                                  updated_at = NOW()
                    RETURNING current_version
                    """,
                    (zone_id,),
                )
                row = cursor.fetchone()
                version = row["current_version"] if row else 1
            else:
                # SQLite: Two-step increment
                cursor.execute(
                    self._fix_sql_placeholders(
                        "SELECT current_version FROM rebac_version_sequences WHERE zone_id = ?"
                    ),
                    (zone_id,),
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
                            WHERE zone_id = ?
                            """
                        ),
                        (new_version, datetime.now(UTC).isoformat(), zone_id),
                    )
                else:
                    # First version for this zone
                    new_version = 1
                    cursor.execute(
                        self._fix_sql_placeholders(
                            """
                            INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                            VALUES (?, ?, ?)
                            """
                        ),
                        (zone_id, new_version, datetime.now(UTC).isoformat()),
                    )

                version = new_version

            conn.commit()
            return f"v{version}"

    def _get_cached_check_zone_aware_bounded(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        zone_id: str,
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
                    WHERE zone_id = ?
                      AND subject_type = ? AND subject_id = ?
                      AND permission = ?
                      AND object_type = ? AND object_id = ?
                      AND computed_at >= ?
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
        zone_id: str,
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
            zone_id: Zone ID to scope all checks
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
            >>> results = manager.rebac_check_bulk(checks, zone_id="org_123")
            >>> # Returns: {check1: True, check2: True, check3: False}
        """
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

        # Note: When zone isolation is disabled, we still use bulk processing
        # but skip the zone_id filter in the SQL query. This provides the same
        # 50-100x speedup as the zone-isolated case. (Issue #580)

        # Validate zone_id (same logic as rebac_check)
        if not zone_id:
            import os

            is_production = (
                os.getenv("NEXUS_ENV") == "production" or os.getenv("ENVIRONMENT") == "production"
            )
            if is_production:
                raise ValueError("zone_id is required for bulk permission checks in production")
            else:
                logger.warning("rebac_check_bulk called without zone_id, defaulting to 'default'")
                zone_id = "default"

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
                    subject[0], subject[1], permission, obj[0], obj[1], zone_id
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

        # PHASE 0.5: Try Tiger Cache for remaining checks (O(1) bitmap lookup)
        # Tiger Cache stores pre-materialized permissions as Roaring Bitmaps
        # OPTIMIZATION: Use bulk Tiger Cache lookup (2 queries total instead of O(N))
        if self._tiger_cache and consistency == ConsistencyLevel.EVENTUAL:
            tiger_start = time_module.perf_counter()
            tiger_hits = 0
            tiger_remaining = []

            # Convert checks to Tiger Cache bulk format
            tiger_checks = [
                (subject[0], subject[1], permission, obj[0], obj[1], zone_id)
                for subject, permission, obj in cache_misses
            ]

            # Bulk check - only 2 DB queries regardless of N items
            tiger_results = self._tiger_cache.check_access_bulk(tiger_checks)

            # Process results
            for check in cache_misses:
                subject, permission, obj = check
                tiger_key = (subject[0], subject[1], permission, obj[0], obj[1], zone_id)
                tiger_result = tiger_results.get(tiger_key)

                if tiger_result is True:
                    results[check] = True
                    tiger_hits += 1
                    # Also populate L1 cache
                    if self._l1_cache is not None:
                        self._l1_cache.set(
                            subject[0], subject[1], permission, obj[0], obj[1], True, zone_id
                        )
                elif tiger_result is None:
                    # Cache miss - need to compute
                    tiger_remaining.append(check)
                else:
                    # Explicit False - still check graph (Tiger Cache may be stale)
                    tiger_remaining.append(check)

            tiger_elapsed = (time_module.perf_counter() - tiger_start) * 1000
            logger.debug(
                f"[BULK-PERF] Tiger Cache BULK: {tiger_hits} hits, {len(tiger_remaining)} remaining in {tiger_elapsed:.1f}ms (2 queries)"
            )

            cache_misses = tiger_remaining
            if not cache_misses:
                total_elapsed = (time_module.perf_counter() - bulk_start) * 1000
                logger.debug(
                    f"[BULK-PERF] ✅ All checks satisfied from L1 + Tiger cache in {total_elapsed:.1f}ms"
                )
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
            all_subjects_list = list(all_subjects)
            all_objects_list = list(all_objects)

            # Build in-memory graph of all tuples (populated by single UNNEST query)
            tuples_graph = []
            now_iso = datetime.now(UTC).isoformat()

            def fetch_all_tuples_single_query(
                entities: list[tuple[str, str]],
            ) -> list[dict]:
                """Fetch ALL tuples for entities in ONE query using UNNEST (PostgreSQL) or VALUES (SQLite).

                PERF FIX: Replaces multiple batched queries with single query.
                - Before: O(N) queries where N = num_entities / BATCH_SIZE
                - After: O(1) query regardless of entity count

                PostgreSQL uses UNNEST for efficient array-based lookup.
                SQLite uses VALUES clause as fallback.
                """
                if not entities:
                    return []

                entity_types = [e[0] for e in entities]
                entity_ids = [e[1] for e in entities]

                is_postgresql = self.engine.dialect.name == "postgresql"

                if is_postgresql:
                    # PostgreSQL: Use UNNEST for efficient bulk lookup (50x faster than temp tables)
                    # See: https://www.atdatabases.org/blog/2022/01/21/optimizing-postgres-using-unnest
                    # Note: Use %s placeholders for psycopg2/psycopg3 compatibility (not $1 asyncpg style)
                    if self.enforce_zone_isolation:
                        query = """
                            WITH entity_list AS (
                                SELECT unnest(%s::text[]) AS entity_type,
                                       unnest(%s::text[]) AS entity_id
                            )
                            SELECT DISTINCT
                                t.subject_type, t.subject_id, t.subject_relation,
                                t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                            FROM rebac_tuples t
                            WHERE t.zone_id = %s
                              AND (t.expires_at IS NULL OR t.expires_at >= %s)
                              AND (
                                  EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                                  OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                              )
                        """
                        params = [entity_types, entity_ids, zone_id, now_iso]
                    else:
                        query = """
                            WITH entity_list AS (
                                SELECT unnest(%s::text[]) AS entity_type,
                                       unnest(%s::text[]) AS entity_id
                            )
                            SELECT DISTINCT
                                t.subject_type, t.subject_id, t.subject_relation,
                                t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                            FROM rebac_tuples t
                            WHERE (t.expires_at IS NULL OR t.expires_at >= %s)
                              AND (
                                  EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                                  OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                              )
                        """
                        params = [entity_types, entity_ids, now_iso]
                else:
                    # SQLite: Use VALUES clause (no UNNEST support)
                    # Build VALUES list: ('file', '/path1'), ('file', '/path2'), ...
                    values_list = ", ".join(["(?, ?)" for _ in entities])
                    value_params: list[str | None] = []
                    for etype, eid in entities:
                        value_params.extend([etype, eid])

                    if self.enforce_zone_isolation:
                        query = self._fix_sql_placeholders(
                            f"""
                            WITH entity_list(entity_type, entity_id) AS (
                                VALUES {values_list}
                            )
                            SELECT DISTINCT
                                t.subject_type, t.subject_id, t.subject_relation,
                                t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                            FROM rebac_tuples t
                            WHERE t.zone_id = ?
                              AND (t.expires_at IS NULL OR t.expires_at >= ?)
                              AND (
                                  EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                                  OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                              )
                            """
                        )
                        value_params.extend([zone_id, now_iso])
                        params = value_params  # type: ignore[assignment]
                    else:
                        query = self._fix_sql_placeholders(
                            f"""
                            WITH entity_list(entity_type, entity_id) AS (
                                VALUES {values_list}
                            )
                            SELECT DISTINCT
                                t.subject_type, t.subject_id, t.subject_relation,
                                t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                            FROM rebac_tuples t
                            WHERE (t.expires_at IS NULL OR t.expires_at >= ?)
                              AND (
                                  EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                                  OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                              )
                            """
                        )
                        value_params.append(now_iso)
                        params = value_params  # type: ignore[assignment]

                cursor.execute(query, params)
                return [
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
                    for row in cursor.fetchall()
                ]

            # PERF FIX: Single query instead of batched loops
            # Combine all subjects and objects into one entity set
            all_entities = list(all_subjects | all_objects)

            logger.debug(
                f"[BULK-UNNEST] Fetching tuples for {len(all_entities)} entities in single query "
                f"(was: {len(all_subjects_list)} subjects + {len(all_objects_list)} objects in batches)"
            )

            fetch_start = time_module.perf_counter()
            tuples_graph = fetch_all_tuples_single_query(all_entities)
            fetch_duration = (time_module.perf_counter() - fetch_start) * 1000

            logger.debug(
                f"[BULK-UNNEST] Fetched {len(tuples_graph)} tuples in {fetch_duration:.1f}ms"
            )

            # CROSS-ZONE FIX: Also fetch cross-zone shares for subjects in the check list
            # Cross-zone shares are stored in the resource owner's zone but need to be
            # visible when checking permissions from the recipient's zone.
            # PERF FIX: Single UNNEST query instead of batched loops
            if self.enforce_zone_isolation and all_subjects_list:
                cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
                is_postgresql = self.engine.dialect.name == "postgresql"

                subject_types = [s[0] for s in all_subjects_list]
                subject_ids = [s[1] for s in all_subjects_list]

                if is_postgresql:
                    # Note: Use %s placeholders for psycopg2/psycopg3 compatibility (not $1 asyncpg style)
                    cross_zone_query = """
                        WITH subject_list AS (
                            SELECT unnest(%s::text[]) AS subject_type,
                                   unnest(%s::text[]) AS subject_id
                        )
                        SELECT DISTINCT
                            t.subject_type, t.subject_id, t.subject_relation,
                            t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                        FROM rebac_tuples t
                        WHERE t.relation = ANY(%s::text[])
                          AND (t.expires_at IS NULL OR t.expires_at >= %s)
                          AND EXISTS (
                              SELECT 1 FROM subject_list s
                              WHERE t.subject_type = s.subject_type AND t.subject_id = s.subject_id
                          )
                    """
                    cross_zone_params: list[str | list[str] | None] = [
                        subject_types,
                        subject_ids,
                        cross_zone_relations,
                        now_iso,
                    ]
                else:
                    # SQLite fallback
                    values_list = ", ".join(["(?, ?)" for _ in all_subjects_list])
                    ct_value_params: list[str | None] = []
                    for stype, sid in all_subjects_list:
                        ct_value_params.extend([stype, sid])

                    relation_placeholders = ", ".join(["?" for _ in cross_zone_relations])
                    cross_zone_query = self._fix_sql_placeholders(
                        f"""
                        WITH subject_list(subject_type, subject_id) AS (
                            VALUES {values_list}
                        )
                        SELECT DISTINCT
                            t.subject_type, t.subject_id, t.subject_relation,
                            t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                        FROM rebac_tuples t
                        WHERE t.relation IN ({relation_placeholders})
                          AND (t.expires_at IS NULL OR t.expires_at >= ?)
                          AND EXISTS (
                              SELECT 1 FROM subject_list s
                              WHERE t.subject_type = s.subject_type AND t.subject_id = s.subject_id
                          )
                        """
                    )
                    ct_value_params.extend(cross_zone_relations)
                    ct_value_params.append(now_iso)
                    cross_zone_params = ct_value_params  # type: ignore[assignment]

                cursor.execute(cross_zone_query, cross_zone_params)
                cross_zone_count = 0
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
                    cross_zone_count += 1

                if cross_zone_count > 0:
                    logger.debug(
                        f"[BULK-UNNEST] Fetched {cross_zone_count} cross-zone tuples in single query"
                    )

                # PR #648: Compute parent relationships in memory (no DB query needed)
                # For files, parent relationship is deterministic from path:
                # - /workspace/project/file.txt → parent → /workspace/project
                # - /workspace/project → parent → /workspace
                # This enables cross-zone folder sharing with children without
                # any additional DB queries or cross-zone complexity.
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
        from nexus.services.permissions.rebac_fast import (
            check_permissions_bulk_with_fallback,
            is_rust_available,
        )

        rust_success = False
        rust_available = is_rust_available()
        logger.warning(
            f"[BULK-DEBUG] cache_misses={len(cache_misses)}, rust_available={rust_available}, tuples_graph={len(tuples_graph)}"
        )

        # Changed threshold from >= 10 to >= 1 to always use Rust when available
        if rust_available and len(cache_misses) >= 1:
            try:
                logger.warning(
                    f"⚡ [BULK-DEBUG] Attempting Rust acceleration for {len(cache_misses)} checks"
                )

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
                    cache_misses,
                    tuples_graph,
                    namespace_configs,
                    force_python=False,
                    tuple_version=self._tuple_version,
                )
                rust_elapsed = time.perf_counter() - rust_start
                per_check_us = (rust_elapsed / len(cache_misses)) * 1_000_000
                logger.debug(
                    f"[RUST-TIMING] {len(cache_misses)} checks in {rust_elapsed * 1000:.1f}ms = {per_check_us:.1f}µs/check"
                )

                # Convert results and cache in L1 (in-memory cache is fast)
                # Calculate per-check delta for XFetch (Issue #718)
                avg_delta = rust_elapsed / len(cache_misses) if cache_misses else 0.0

                l1_cache_writes = 0
                for check in cache_misses:
                    subject, permission, obj = check
                    key = (subject[0], subject[1], permission, obj[0], obj[1])
                    result = rust_results_dict.get(key, False)
                    results[check] = result

                    # Write to L1 in-memory cache with XFetch delta (fast, ~0.01ms per write)
                    if self._l1_cache is not None:
                        self._l1_cache.set(
                            subject[0],
                            subject[1],
                            permission,
                            obj[0],
                            obj[1],
                            result,
                            zone_id,
                            delta=avg_delta,
                        )
                        l1_cache_writes += 1

                if l1_cache_writes > 0:
                    logger.debug(
                        f"[RUST-PERF] Wrote {l1_cache_writes} results to L1 in-memory cache"
                    )

                # Write-through to Tiger Cache (Issue #935)
                # Only cache positive (allowed) results to Tiger Cache bitmaps
                if self._tiger_cache and zone_id:
                    tiger_writes = 0
                    # Group results by (subject, permission, resource_type) for efficient bulk updates
                    tiger_updates: dict[
                        tuple[str, str, str, str, str], set[int]
                    ] = {}  # (subj_type, subj_id, perm, res_type, zone) -> set of int_ids

                    for check in cache_misses:
                        subject, permission, obj = check
                        key = (subject[0], subject[1], permission, obj[0], obj[1])
                        result = rust_results_dict.get(key, False)

                        if result:  # Only cache positive results
                            try:
                                # Get or create integer ID for the resource
                                resource_int_id = (
                                    self._tiger_cache._resource_map.get_or_create_int_id(
                                        obj[0], obj[1], zone_id
                                    )
                                )
                                if resource_int_id > 0:
                                    # Group by (subject, permission, resource_type) for bulk add
                                    group_key = (
                                        subject[0],
                                        subject[1],
                                        permission,
                                        obj[0],
                                        zone_id,
                                    )
                                    if group_key not in tiger_updates:
                                        tiger_updates[group_key] = set()
                                    tiger_updates[group_key].add(resource_int_id)
                                    tiger_writes += 1
                            except Exception as e:
                                logger.debug(f"[TIGER] Failed to get int_id for {obj}: {e}")

                    # Bulk add to Tiger Cache bitmaps (memory)
                    for group_key, int_ids in tiger_updates.items():
                        subj_type, subj_id, perm, res_type, tid = group_key
                        self._tiger_cache.add_to_bitmap_bulk(
                            subject_type=subj_type,
                            subject_id=subj_id,
                            permission=perm,
                            resource_type=res_type,
                            zone_id=tid,
                            resource_int_ids=int_ids,
                        )

                    # Persist to database in background (Issue #979)
                    tiger_cache = self._tiger_cache  # Capture reference for closure

                    def _persist_tiger_updates(updates: dict, cache: Any) -> None:
                        for gkey, ids in updates.items():
                            st, si, p, rt, t = gkey
                            try:
                                cache.persist_bitmap_bulk(
                                    subject_type=st,
                                    subject_id=si,
                                    permission=p,
                                    resource_type=rt,
                                    resource_int_ids=ids,
                                    zone_id=t,
                                )
                            except Exception as e:
                                logger.debug(f"[TIGER] Background persist failed: {e}")

                    threading.Thread(
                        target=_persist_tiger_updates,
                        args=(tiger_updates.copy(), tiger_cache),
                        daemon=True,
                    ).start()

                    if tiger_writes > 0:
                        logger.debug(
                            f"[TIGER] Write-through: {tiger_writes} positive results "
                            f"to {len(tiger_updates)} Tiger Cache bitmaps (async persist started)"
                        )

                rust_success = True
                logger.warning(
                    f"✅ [BULK-DEBUG] Rust acceleration successful for {len(cache_misses)} checks"
                )

            except Exception as e:
                logger.warning(
                    f"[BULK-DEBUG] Rust acceleration failed: {e}, falling back to Python"
                )
                rust_success = False

        # FALLBACK TO PYTHON if Rust not available or failed
        if not rust_success:
            logger.warning(
                f"🐍 [BULK-DEBUG] Using SLOW Python path for {len(cache_misses)} checks (rust_available={rust_available})"
            )
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
                        zone_id,
                        tuples_graph,
                        bulk_memo_cache=bulk_memo_cache,  # Pass shared memo cache
                        memo_stats=memo_stats,  # Pass stats tracker
                    )
                except Exception as e:
                    logger.warning(f"Bulk check failed for {check}, falling back: {e}")
                    # Fallback to individual check
                    result = self.rebac_check(
                        subject, permission, obj, zone_id=zone_id, consistency=consistency
                    )

                results[check] = result

                # Cache the result if using EVENTUAL consistency
                if consistency == ConsistencyLevel.EVENTUAL:
                    self._cache_check_result(
                        subject_entity, permission, obj_entity, result, zone_id
                    )

            # Write-through to Tiger Cache after Python fallback (Issue #935)
            # Collect all positive results and bulk update Tiger Cache
            if self._tiger_cache and zone_id:
                tiger_writes = 0
                fallback_tiger_updates: dict[
                    tuple[str, str, str, str, str], set[int]
                ] = {}  # (subj_type, subj_id, perm, res_type, zone) -> set of int_ids

                for check in cache_misses:
                    subject, permission, obj = check
                    result = results.get(check, False)

                    if result:  # Only cache positive results
                        try:
                            resource_int_id = self._tiger_cache._resource_map.get_or_create_int_id(
                                obj[0], obj[1], zone_id
                            )
                            if resource_int_id > 0:
                                group_key = (
                                    subject[0],
                                    subject[1],
                                    permission,
                                    obj[0],
                                    zone_id,
                                )
                                if group_key not in fallback_tiger_updates:
                                    fallback_tiger_updates[group_key] = set()
                                fallback_tiger_updates[group_key].add(resource_int_id)
                                tiger_writes += 1
                        except Exception as e:
                            logger.debug(f"[TIGER] Failed to get int_id for {obj}: {e}")

                # Bulk add to Tiger Cache bitmaps (memory)
                for group_key, int_ids in fallback_tiger_updates.items():
                    subj_type, subj_id, perm, res_type, tid = group_key
                    self._tiger_cache.add_to_bitmap_bulk(
                        subject_type=subj_type,
                        subject_id=subj_id,
                        permission=perm,
                        resource_type=res_type,
                        zone_id=tid,
                        resource_int_ids=int_ids,
                    )

                # Persist to database in background (Issue #979)
                fallback_tiger_cache = self._tiger_cache  # Capture reference for closure

                def _persist_fallback_updates(updates: dict, cache: Any) -> None:
                    for gkey, ids in updates.items():
                        st, si, p, rt, t = gkey
                        try:
                            cache.persist_bitmap_bulk(
                                subject_type=st,
                                subject_id=si,
                                permission=p,
                                resource_type=rt,
                                resource_int_ids=ids,
                                zone_id=t,
                            )
                        except Exception as e:
                            logger.debug(f"[TIGER] Background persist failed: {e}")

                threading.Thread(
                    target=_persist_fallback_updates,
                    args=(fallback_tiger_updates.copy(), fallback_tiger_cache),
                    daemon=True,
                ).start()

                if tiger_writes > 0:
                    logger.debug(
                        f"[TIGER] Write-through (Python path): {tiger_writes} positive results "
                        f"to {len(fallback_tiger_updates)} Tiger Cache bitmaps (async persist started)"
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
        zone_id: str | None = None,
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
            zone_id: Zone ID for multi-zone isolation
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
            ...     zone_id="org_123",
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
        import time as time_module

        from nexus.services.permissions.rebac_fast import (
            RUST_AVAILABLE,
            list_objects_for_subject_rust,
        )
        start_time = time_module.perf_counter()

        subject_type, subject_id = subject
        zone_id = normalize_zone_id(zone_id)

        logger.debug(
            f"[LIST-OBJECTS] Starting for {subject_type}:{subject_id} "
            f"permission={permission} object_type={object_type} "
            f"path_prefix={path_prefix} zone_id={zone_id}"
        )

        # Fetch all relevant tuples for this zone
        # This includes direct relations, group memberships, etc.
        # CROSS-ZONE FIX: Include cross-zone shares where this user is the recipient
        tuples = self._fetch_tuples_for_zone(zone_id, include_cross_zone_for_user=subject_id)
        logger.debug(f"[LIST-OBJECTS] Fetched {len(tuples)} tuples for zone {zone_id}")

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
            zone_id=zone_id,
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
        zone_id: str,
        tuples: list[dict[str, Any]],
        _namespace_configs: dict[str, Any],
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        """Python fallback implementation for rebac_list_objects.

        Slower than Rust but provides same functionality when Rust is not available.
        """
        import time as time_module
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
                zone_id=zone_id,
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

    def _fetch_tuples_for_zone(
        self, zone_id: str, include_cross_zone_for_user: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch all ReBAC tuples for a zone, optionally including cross-zone shares.

        This is used by rebac_list_objects to get the full tuple graph.

        Args:
            zone_id: The zone ID to fetch tuples for
            include_cross_zone_for_user: If provided, also include cross-zone shares
                where this user is the recipient (subject). This enables users to see
                resources shared with them from other zones.

        Returns:
            List of tuple dictionaries for graph traversal
        """
        from sqlalchemy import bindparam, text

        with self.engine.connect() as conn:
            if include_cross_zone_for_user:
                # Include same-zone tuples AND cross-zone shares to this user
                # Cross-zone shares have relation in CROSS_ZONE_ALLOWED_RELATIONS
                cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
                # Use bindparam with expanding=True for IN clause compatibility with SQLite
                result = conn.execute(
                    text("""
                        SELECT subject_type, subject_id, subject_relation,
                               relation, object_type, object_id
                        FROM rebac_tuples
                        WHERE (expires_at IS NULL OR expires_at > :now)
                          AND (
                              -- Same zone tuples
                              zone_id = :zone_id
                              -- OR cross-zone shares where this user is the recipient
                              OR (
                                  relation IN :cross_zone_relations
                                  AND subject_type = 'user'
                                  AND subject_id = :user_id
                              )
                          )
                    """).bindparams(bindparam("cross_zone_relations", expanding=True)),
                    {
                        "zone_id": zone_id,
                        "now": datetime.now(UTC),
                        "cross_zone_relations": cross_zone_relations,
                        "user_id": include_cross_zone_for_user,
                    },
                )
            else:
                # Original behavior: only same-zone tuples
                result = conn.execute(
                    text("""
                        SELECT subject_type, subject_id, subject_relation,
                               relation, object_type, object_id
                        FROM rebac_tuples
                        WHERE zone_id = :zone_id
                          AND (expires_at IS NULL OR expires_at > :now)
                    """),
                    {"zone_id": zone_id, "now": datetime.now(UTC)},
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
        for obj_type in ["file", "group", "zone", "memory"]:
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
        zone_id: str,
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
            zone_id: Zone ID
            tuples_graph: Pre-fetched list of all relevant tuples
            depth: Current traversal depth (for cycle detection)
            visited: Set of visited nodes (for cycle detection)
            bulk_memo_cache: Shared memoization cache for bulk operations (optimization)

        Returns:
            True if permission is granted
        """

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
                    zone_id,
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
                    zone_id,
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
                    zone_id,
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
                    zone_id,
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
                        zone_id,
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
                        zone_id,
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
