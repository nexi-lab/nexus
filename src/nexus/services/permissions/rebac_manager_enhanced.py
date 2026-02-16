"""
Enhanced ReBAC Manager with P0 Fixes

This module implements critical security and reliability fixes for GA:
- P0-1: Consistency levels and version tokens
- P0-2: Zone scoping (absorbed from ZoneAwareReBACManager — Phase 10)
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

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.exc import OperationalError, ProgrammingError

from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS, Entity, NamespaceConfig
from nexus.services.permissions.batch.bulk_checker import BulkPermissionChecker
from nexus.services.permissions.cache.tiger.facade import TigerFacade
from nexus.services.permissions.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)
from nexus.services.permissions.consistency.zone_manager import ZoneIsolationValidator
from nexus.services.permissions.directory.expander import DirectoryExpander
from nexus.services.permissions.graph.bulk_evaluator import (
    check_direct_relation as _check_direct_relation_in_graph,
)
from nexus.services.permissions.graph.bulk_evaluator import (
    compute_permission as _compute_permission_bulk,
)
from nexus.services.permissions.graph.bulk_evaluator import (
    find_related_objects as _find_related_objects_in_graph,
)
from nexus.services.permissions.graph.bulk_evaluator import (
    find_subjects as _find_subjects_in_graph,
)
from nexus.services.permissions.graph.zone_traversal import ZoneAwareTraversal
from nexus.services.permissions.rebac_manager import ReBACManager
from nexus.services.permissions.rebac_tracing import (
    record_check_result,
    record_graph_limit_exceeded,
    record_traversal_result,
    start_check_span,
    start_graph_traversal_span,
)
from nexus.services.permissions.types import (
    CheckResult,
    ConsistencyLevel,
    ConsistencyMode,  # noqa: F401 — re-exported for backward compatibility
    ConsistencyRequirement,
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
    WriteResult,
)
from nexus.services.permissions.utils.changelog import insert_changelog_entry
from nexus.services.permissions.utils.zone import normalize_zone_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.services.permissions.leopard import LeopardIndex
    from nexus.services.permissions.rebac_iterator_cache import IteratorCache
    from nexus.services.permissions.tiger_cache import TigerCache, TigerCacheUpdater

logger = logging.getLogger(__name__)


# ============================================================================
# Enhanced ReBAC Manager (All P0 Fixes Integrated)
# ============================================================================


class EnhancedReBACManager(ReBACManager):
    """ReBAC Manager with all P0 fixes integrated.

    Combines:
    - P0-1: Consistency levels and version tokens
    - P0-2: Zone scoping (absorbed from ZoneAwareReBACManager — Phase 10)
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
        super().__init__(engine, cache_ttl_seconds, max_depth)
        # Zone isolation (absorbed from ZoneAwareReBACManager — Phase 10)
        self.enforce_zone_isolation = enforce_zone_isolation
        self._zone_manager = ZoneIsolationValidator(enforce=enforce_zone_isolation)
        self.enable_graph_limits = enable_graph_limits
        self.enable_leopard = enable_leopard
        self.enable_tiger_cache = enable_tiger_cache
        # REMOVED: self._version_counter (replaced with DB sequence in Issue #2 fix)

        # PERFORMANCE FIX: Cache zone tuples to avoid O(T) fetch per permission check
        # Key: zone_id, Value: (tuples_list, namespace_configs, cached_at_timestamp)
        # This dramatically reduces DB queries: from O(T) per check to O(1) amortized
        # Issue #1459: LRU-capped to max 100 zones to prevent unbounded memory growth
        from cachetools import LRUCache

        self._zone_graph_cache: LRUCache[
            str, tuple[list[dict[str, Any]], dict[str, Any], float]
        ] = LRUCache(maxsize=100)
        self._zone_graph_cache_ttl = cache_ttl_seconds  # Reuse existing TTL
        self._zone_graph_cache_lock = threading.RLock()

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

        # Issue #1459 Phase 12: Tiger Cache facade
        self._tiger_facade = TigerFacade(
            tiger_cache=self._tiger_cache,
            tiger_updater=self._tiger_updater,
        )

        # Issue #1459 Phase 13: Directory permission expander (Leopard-style)
        self._directory_expander = DirectoryExpander(
            engine=engine,
            tiger_cache=self._tiger_cache,
        )

        # Issue #1459 Phase 15+: Zone-aware graph traversal
        self._zone_traversal = ZoneAwareTraversal(
            connection_factory=self._connection,
            create_cursor=self._create_cursor,
            fix_sql=self._fix_sql_placeholders,
            get_namespace=self.get_namespace,
            evaluate_conditions=self._evaluate_conditions,
            zone_manager=self._zone_manager,
            enable_graph_limits=enable_graph_limits,
        )

        # Issue #1459 Phase 15+: Bulk permission checker
        self._bulk_checker = BulkPermissionChecker(
            engine=engine,
            connection_factory=self._connection,
            create_cursor=self._create_cursor,
            fix_sql=self._fix_sql_placeholders,
            get_namespace=self.get_namespace,
            enforce_zone_isolation=enforce_zone_isolation,
            l1_cache=self._l1_cache,
            tiger_cache=self._tiger_cache,
            compute_bulk_helper=self._compute_permission_bulk_helper,
            rebac_check_single=self.rebac_check,
            cache_result=self._cache_check_result,
            tuple_version=getattr(self, "_tuple_version", 0),
        )

        # Iterator cache for paginated list operations (Issue #722)
        from nexus.services.permissions.rebac_iterator_cache import IteratorCache

        self._iterator_cache: IteratorCache = IteratorCache(
            max_size=1000,
            ttl_seconds=cache_ttl_seconds,
        )

        # Issue #922: Permission boundary cache for O(1) inheritance checks
        from nexus.services.permissions.permission_boundary_cache import PermissionBoundaryCache

        self._boundary_cache: PermissionBoundaryCache = PermissionBoundaryCache()

        # Issue #922/#919: Cache invalidation callbacks — now managed by CacheCoordinator
        # Kept for backward compatibility with code that reads these lists directly.
        self._boundary_cache_invalidators: list[
            tuple[str, Any]  # (callback_id, callback_fn)
        ] = []
        self._dir_visibility_invalidators: list[
            tuple[str, Any]  # (callback_id, callback_fn)
        ] = []

        # Issue #1459: Unified cache coordinator
        from nexus.services.permissions.cache.coordinator import CacheCoordinator

        self._cache_coordinator: CacheCoordinator = CacheCoordinator(
            l1_cache=self._l1_cache,
            boundary_cache=self._boundary_cache,
            iterator_cache=self._iterator_cache,
            zone_graph_cache=self._zone_graph_cache,
        )

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

        # Issue #702: OTel tracing — wrap the entire check in a root span
        check_start = time.perf_counter()
        with start_check_span(
            subject=subject,
            permission=permission,
            obj=object,
            zone_id=zone_id,
            consistency=consistency_level.value,
        ) as _check_span:
            result = self._rebac_check_inner(
                subject,
                permission,
                object,
                context,
                zone_id,
                consistency_level,
                min_revision,
            )
            decision_ms = (time.perf_counter() - check_start) * 1000
            record_check_result(_check_span, allowed=result, decision_time_ms=decision_ms)
            return result

    def _rebac_check_inner(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None,
        zone_id: str | None,
        consistency_level: ConsistencyLevel,
        min_revision: int | None,
    ) -> bool:
        """Inner body of rebac_check, extracted for clean span wrapping (Issue #702)."""
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
            return self._fresh_compute(
                subject_entity, permission, object_entity, zone_id, start_time, context
            )

        elif consistency == ConsistencyLevel.BOUNDED:
            # Bounded consistency: Max 1s staleness OR revision-based (Issue #1081)
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

            # Cache miss or too old/stale - compute fresh and cache
            result = self._fresh_compute(
                subject_entity, permission, object_entity, zone_id, start_time, context
            )
            self._cache_check_result_zone_aware(
                subject_entity, permission, object_entity, zone_id, result.allowed
            )
            return result

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

            # Cache miss - compute fresh and cache
            result = self._fresh_compute(
                subject_entity, permission, object_entity, zone_id, start_time, context
            )
            self._cache_check_result_zone_aware(
                subject_entity, permission, object_entity, zone_id, result.allowed
            )
            return result

    def _fresh_compute(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        zone_id: str,
        start_time: float,
        context: dict[str, Any] | None = None,
    ) -> CheckResult:
        """Compute a fresh permission check with graph limits (Issue #702 DRY refactor).

        Encapsulates the common pattern: allocate TraversalStats, call
        ``_compute_permission_with_limits``, handle ``GraphLimitExceeded``,
        and assemble a ``CheckResult``.
        """
        stats = TraversalStats()
        limit_error: GraphLimitExceeded | None = None

        # Issue #702: Wrap graph computation in a traversal span
        with start_graph_traversal_span(engine="python") as _trav_span:
            try:
                result = self._compute_permission_with_limits(
                    subject, permission, obj, zone_id, stats, context
                )
            except GraphLimitExceeded as e:
                logger.error(
                    "GraphLimitExceeded caught: limit_type=%s, limit_value=%s, actual_value=%s",
                    e.limit_type,
                    e.limit_value,
                    e.actual_value,
                )
                result = False
                limit_error = e
                record_graph_limit_exceeded(_trav_span, limit_type=e.limit_type)

            record_traversal_result(
                _trav_span,
                depth=stats.max_depth_reached,
                visited_nodes=stats.nodes_visited,
                db_queries=stats.queries,
                cache_hits=stats.cache_hits,
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

        except (RuntimeError, ValueError) as e:
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
        with self._zone_graph_cache_lock:
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
        with self._zone_graph_cache_lock:
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
                    LIMIT 10000
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

        with self._zone_graph_cache_lock:
            if zone_id is None:
                count = len(self._zone_graph_cache)
                self._zone_graph_cache.clear()
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[GRAPH-CACHE] Cleared all %d cached zone graphs", count)
            elif zone_id in self._zone_graph_cache:
                del self._zone_graph_cache[zone_id]
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[GRAPH-CACHE] Invalidated cache for zone %s", zone_id)

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
        # Also register with cache coordinator (Issue #1459)
        self._cache_coordinator.register_boundary_invalidator(callback_id, callback)

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
            except (RuntimeError, ValueError, KeyError) as e:
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
        # Also register with cache coordinator (Issue #1459)
        self._cache_coordinator.register_visibility_invalidator(callback_id, callback)

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

    # =========================================================================
    # Issue #1244: Namespace Cache Invalidation
    # =========================================================================

    def register_namespace_invalidator(
        self,
        callback_id: str,
        callback: Callable[[str, str, str], None],
    ) -> None:
        """Register a callback to invalidate namespace cache on permission changes.

        This allows NamespaceManager's dcache + mount table to be immediately
        invalidated when permission tuples are written or deleted, rather than
        waiting for revision bucket rollover or TTL expiry.

        Args:
            callback_id: Unique identifier for this callback (for deregistration)
            callback: Function(subject_type, subject_id, zone_id) that invalidates
                the subject's dcache and mount table entries

        Example:
            >>> def invalidator(subject_type, subject_id, zone_id):
            ...     namespace_manager.invalidate((subject_type, subject_id))
            >>> manager.register_namespace_invalidator("namespace", invalidator)
        """
        self._cache_coordinator.register_namespace_invalidator(callback_id, callback)

    def unregister_namespace_invalidator(self, callback_id: str) -> bool:
        """Unregister a namespace cache invalidation callback."""
        return self._cache_coordinator.unregister_namespace_invalidator(callback_id)

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
            except (RuntimeError, ValueError, KeyError) as e:
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

        # Zone-aware tuple insertion (absorbed from ZoneAwareReBACManager — Phase 10)
        result = self._write_tuple_zone_aware(
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
            except (OperationalError, ProgrammingError) as e:
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

        # Issue #1244: Notify namespace cache invalidators (dcache + mount table)
        self._cache_coordinator.notify_namespace_invalidators(
            effective_zone, subject[0], subject[1]
        )

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
                        except (OperationalError, ProgrammingError) as e:
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

            # Issue #1244: Notify namespace cache invalidators (dcache + mount table)
            for t in tuples:
                subject = t["subject"]
                zone_id = normalize_zone_id(t.get("zone_id"))
                self._cache_coordinator.notify_namespace_invalidators(
                    zone_id, subject[0], subject[1]
                )

        return created_count

    # ============================================================================
    # Zone-Aware Methods (Absorbed from ZoneAwareReBACManager — Phase 10)
    # ============================================================================

    def _write_tuple_zone_aware(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
        subject_zone_id: str | None = None,
        object_zone_id: str | None = None,
    ) -> str:
        """Insert a relationship tuple with zone isolation.

        Handles zone validation, subject parsing, tuple insertion, changelog
        logging, and cache invalidation. Returns the tuple ID.

        If zone isolation is disabled, delegates to ReBACManager.rebac_write.
        """
        # Ensure default namespaces are initialized
        self._ensure_namespaces_initialized()

        # If zone isolation is disabled, use base ReBACManager implementation
        if not self.enforce_zone_isolation:
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

        # Delegate zone validation to ZoneIsolationValidator (Issue #1459)
        zone_id, subject_zone_id, object_zone_id, _is_cross_zone = (
            self._zone_manager.validate_write_zones(
                zone_id, subject_zone_id, object_zone_id, relation
            )
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
                return cast(
                    str, existing[0] if isinstance(existing, tuple) else existing["tuple_id"]
                )

            # Insert tuple with zone_id columns
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
                    subject_relation,
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

            # Log to changelog
            insert_changelog_entry(
                cursor,
                self._fix_sql_placeholders,
                change_type="INSERT",
                tuple_id=tuple_id,
                subject_type=subject_entity.entity_type,
                subject_id=subject_entity.entity_id,
                relation=relation,
                object_type=object_entity.entity_type,
                object_id=object_entity.entity_id,
                zone_id=zone_id,
            )

            conn.commit()

            # Invalidate cache entries affected by this change
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
            # cache for the subject's zone
            if subject_zone_id != zone_id:
                self._invalidate_cache_for_tuple(
                    subject_entity,
                    relation,
                    object_entity,
                    subject_zone_id,
                    subject_relation,
                    expires_at,
                    conn=conn,
                )

        return tuple_id

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str = "default",
    ) -> list[tuple[str, str]]:
        """Find all subjects with permission on object (zone-scoped).

        Args:
            permission: Permission to check
            object: (object_type, object_id) tuple
            zone_id: Zone ID to scope expansion

        Returns:
            List of (subject_type, subject_id) tuples within zone
        """
        # If zone isolation is disabled, use base ReBACManager implementation
        if not self.enforce_zone_isolation:
            return ReBACManager.rebac_expand(self, permission, object)

        if not zone_id:
            zone_id = "default"

        object_entity = Entity(object[0], object[1])
        subjects: set[tuple[str, str]] = set()

        # Get namespace config
        namespace = self.get_namespace(object_entity.entity_type)
        if not namespace:
            return self._get_direct_subjects_zone_aware(permission, object_entity, zone_id)

        # Recursively expand permission via namespace config (zone-scoped)
        self._expand_permission_zone_aware(
            permission, object_entity, namespace, zone_id, subjects, visited=set(), depth=0
        )

        return list(subjects)

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
        if depth > self.max_depth:
            return

        visit_key = (permission, obj.entity_type, obj.entity_id)
        if visit_key in visited:
            return
        visited.add(visit_key)

        rel_config = namespace.get_relation_config(permission)
        if not rel_config:
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

                related_objects = self._find_related_objects_zone_aware(
                    obj, tupleset_relation, zone_id
                )

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

        # Direct relation
        direct_subjects = self._get_direct_subjects_zone_aware(permission, obj, zone_id)
        for subj in direct_subjects:
            subjects.add(subj)

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

    # ============================================================================
    # End Zone-Aware Methods
    # ============================================================================

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
                        except (OperationalError, ProgrammingError) as e:
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
                except (OperationalError, ProgrammingError) as e:
                    # Log but don't fail the delete - closure can be rebuilt
                    logger.warning(f"[LEOPARD] Failed to update closure on delete: {e}")

            # Issue #919: Notify directory visibility cache invalidators
            object_tuple = (tuple_info["object_type"], tuple_info["object_id"])
            self._notify_dir_visibility_invalidators(normalize_zone_id(zone_id), object_tuple)

            # Issue #1244: Notify namespace cache invalidators (dcache + mount table)
            self._cache_coordinator.notify_namespace_invalidators(
                normalize_zone_id(zone_id),
                tuple_info["subject_type"],
                tuple_info["subject_id"],
            )

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
        except (RuntimeError, ValueError, KeyError) as e:
            # Don't fail the permission check if Tiger write fails
            if logger:
                logger.debug(f"[TIGER] Write-through failed: {e}")

    def get_cached_permission(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool | None:
        """Query L1 caches without hitting the database (Issue #726).

        Used by the circuit breaker fallback to serve stale-but-available
        permission results when the database is unreachable.

        Checks Tiger Cache (bitmap) and Boundary Cache (path inheritance)
        in order.  Returns None on cache miss (no DB fallback).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            zone_id: Zone ID (used for boundary cache lookups)

        Returns:
            True if permission is cached as granted, None if not in cache.
            Does NOT return False — a cache miss is not a denial.
        """
        # Try Tiger Cache first (O(1) bitmap lookup)
        if self._tiger_cache:
            tiger_result = self._tiger_cache.check_access(
                subject_type=subject[0],
                subject_id=subject[1],
                permission=permission,
                resource_type=object[0],
                resource_id=object[1],
            )
            if tiger_result is True:
                return True

        # Try Boundary Cache (O(1) inheritance shortcut for files)
        effective_zone = zone_id or "default"
        if (
            object[0] == "file"
            and permission in ("read", "write", "execute")
            and self._boundary_cache
        ):
            boundary = self._boundary_cache.get_boundary(
                effective_zone, subject[0], subject[1], permission, object[1]
            )
            if boundary:
                return True

        return None

    # =========================================================================
    # Tiger Cache Operations (Issue #1459 Phase 12: delegated to TigerFacade)
    # =========================================================================

    def tiger_check_access(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        _zone_id: str = "",
    ) -> bool | None:
        """Check permission using Tiger Cache (O(1) bitmap lookup)."""
        return self._tiger_facade.check_access(subject, permission, object)

    def tiger_get_accessible_resources(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> set[int]:
        """Get all resources accessible by subject using Tiger Cache."""
        return self._tiger_facade.get_accessible_resources(
            subject, permission, resource_type, zone_id
        )

    def tiger_queue_update(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        zone_id: str,
        priority: int = 100,
    ) -> int | None:
        """Queue a Tiger Cache update for background processing."""
        return self._tiger_facade.queue_update(
            subject, permission, resource_type, zone_id, priority
        )

    def tiger_persist_grant(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> bool:
        """Write-through: Persist a single permission grant to Tiger Cache."""
        return self._tiger_facade.persist_grant(
            subject, permission, resource_type, resource_id, zone_id
        )

    def tiger_persist_revoke(
        self,
        subject: tuple[str, str],
        permission: str,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> bool:
        """Write-through: Persist a single permission revocation to Tiger Cache."""
        return self._tiger_facade.persist_revoke(
            subject, permission, resource_type, resource_id, zone_id
        )

    def tiger_process_queue(self, batch_size: int = 100) -> int:
        """Process pending Tiger Cache update queue."""
        return self._tiger_facade.process_queue(batch_size)

    def tiger_invalidate_cache(
        self,
        subject: tuple[str, str] | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate Tiger Cache entries."""
        return self._tiger_facade.invalidate_cache(subject, permission, resource_type, zone_id)

    def tiger_register_resource(
        self,
        resource_type: str,
        resource_id: str,
        _zone_id: str = "",
    ) -> int:
        """Register a resource in the Tiger resource map."""
        return self._tiger_facade.register_resource(resource_type, resource_id)

    # =========================================================================
    # Directory Operations (Issue #1459 Phase 13: delegated to DirectoryExpander)
    # =========================================================================

    # Kept for backward compatibility — class attribute referenced by some tests
    DIRECTORY_EXPANSION_LIMIT = 10_000

    def _is_directory_path(self, path: str) -> bool:
        """Check if a path represents a directory."""
        return self._directory_expander.is_directory_path(path)

    def _expand_directory_permission_grant(
        self,
        subject: tuple[str, str],
        permissions: list[str],
        directory_path: str,
        zone_id: str,
    ) -> None:
        """Expand a directory permission grant to all descendants (Leopard-style)."""
        self._directory_expander.expand_directory_permission_grant(
            subject, permissions, directory_path, zone_id
        )

    def _get_zone_revision_for_grant(self, zone_id: str) -> int:
        """Get current zone revision for consistency during expansion."""
        return get_zone_revision_for_grant(self.engine, zone_id)

    def _get_directory_descendants(self, directory_path: str, zone_id: str) -> list[str]:
        """Get all file paths under a directory."""
        return self._directory_expander.get_directory_descendants(directory_path, zone_id)

    def set_metadata_store(self, metadata_store: Any) -> None:
        """Set the metadata store reference for directory queries."""
        self._directory_expander.set_metadata_store(metadata_store)

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

        Delegates to ZoneAwareTraversal (Issue #1459 Phase 15+).
        """
        return self._zone_traversal.compute_permission(
            subject,
            permission,
            obj,
            zone_id,
            visited,
            depth,
            start_time,
            stats,
            context,
            memo,
        )

    def _find_related_objects_zone_aware(
        self, obj: Entity, relation: str, zone_id: str
    ) -> list[Entity]:
        """Find related objects (zone-scoped). Delegates to ZoneAwareTraversal."""
        return self._zone_traversal.find_related_objects(obj, relation, zone_id)

    def _find_subjects_with_relation_zone_aware(
        self, obj: Entity, relation: str, zone_id: str
    ) -> list[Entity]:
        """Find subjects with relation (zone-scoped). Delegates to ZoneAwareTraversal."""
        return self._zone_traversal.find_subjects(obj, relation, zone_id)

    def _has_direct_relation_zone_aware(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Check direct relation (zone-scoped). Delegates to ZoneAwareTraversal."""
        return self._zone_traversal.has_direct_relation(
            subject,
            relation,
            obj,
            zone_id,
            context,
        )

    def _get_version_token(self, zone_id: str = "default") -> str:
        """Get current version token (P0-1).

        Delegates to consistency.revision module (Issue #1459).
        """
        return increment_version_token(self.engine, self._repo, zone_id)

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

        Delegates to BulkPermissionChecker (Issue #1459 Phase 15+).

        Performance impact: 100x reduction in database queries for N=20 objects.
        - Before: 20 files * 15 queries/file = 300 queries
        - After: 1-2 queries to fetch all tuples + in-memory computation
        """
        # Keep mutable refs in sync before delegating
        self._bulk_checker.update_refs(
            l1_cache=self._l1_cache,
            tiger_cache=self._tiger_cache,
            tuple_version=getattr(self, "_tuple_version", 0),
        )
        return self._bulk_checker.check_bulk(checks, zone_id, consistency)

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
            except (RuntimeError, ValueError) as e:
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

    # =========================================================================
    # Bulk Graph Evaluator (Issue #1459 Phase 11: delegated to graph.bulk_evaluator)
    # =========================================================================

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
        """Compute permission using pre-fetched tuples graph with full in-memory traversal."""
        return _compute_permission_bulk(
            subject=subject,
            permission=permission,
            obj=obj,
            zone_id=zone_id,
            tuples_graph=tuples_graph,
            get_namespace=self.get_namespace,
            depth=depth,
            visited=visited,
            bulk_memo_cache=bulk_memo_cache,
            memo_stats=memo_stats,
        )

    def _check_direct_relation_in_graph(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        tuples_graph: list[dict[str, Any]],
    ) -> bool:
        """Check if a direct relation tuple exists in the pre-fetched graph."""
        return _check_direct_relation_in_graph(subject, permission, obj, tuples_graph)

    def _find_related_objects_in_graph(
        self,
        obj: Entity,
        tupleset_relation: str,
        tuples_graph: list[dict[str, Any]],
    ) -> list[Entity]:
        """Find all objects related to obj via tupleset_relation in the graph."""
        return _find_related_objects_in_graph(obj, tupleset_relation, tuples_graph)

    def _find_subjects_in_graph(
        self,
        obj: Entity,
        tupleset_relation: str,
        tuples_graph: list[dict[str, Any]],
    ) -> list[Entity]:
        """Find all subjects that have a relation to obj in the graph."""
        return _find_subjects_in_graph(obj, tupleset_relation, tuples_graph)

    # =========================================================================
    # Public Batch Delete API
    # =========================================================================

    def rebac_delete_by_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str | None = None,
    ) -> int:
        """Delete all ReBAC tuples for a given subject.

        Uses batch DELETE + changelog INSERT in a single transaction.
        This is the proper public API for bulk subject removal (e.g., revoking
        all permissions for a delegated agent).

        Args:
            subject_type: Subject type (e.g., "agent", "user")
            subject_id: Subject identifier
            zone_id: Optional zone scope (normalized internally)

        Returns:
            Number of tuples deleted
        """
        from datetime import UTC, datetime

        from nexus.services.permissions.utils.zone import normalize_zone_id

        normalized_zone = normalize_zone_id(zone_id)
        now = datetime.now(UTC).isoformat()
        fix = self._fix_sql_placeholders

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # 1. SELECT all tuples to capture for changelog
            select_q = (
                "SELECT tuple_id, subject_type, subject_id, relation, "
                "object_type, object_id, zone_id "
                "FROM rebac_tuples "
                "WHERE subject_type = ? AND subject_id = ?"
            )
            params: list = [subject_type, subject_id]
            if normalized_zone:
                select_q += " AND zone_id = ?"
                params.append(normalized_zone)
            cursor.execute(fix(select_q), params)
            rows = cursor.fetchall()

            if not rows:
                return 0

            # 2. Batch DELETE
            delete_q = "DELETE FROM rebac_tuples WHERE subject_type = ? AND subject_id = ?"
            delete_params: list = [subject_type, subject_id]
            if normalized_zone:
                delete_q += " AND zone_id = ?"
                delete_params.append(normalized_zone)
            cursor.execute(fix(delete_q), delete_params)

            # 3. Batch INSERT changelog entries
            for row in rows:
                cursor.execute(
                    fix(
                        "INSERT INTO rebac_changelog ("
                        "  change_type, tuple_id, subject_type, subject_id,"
                        "  relation, object_type, object_id, zone_id, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        "DELETE",
                        row["tuple_id"],
                        row["subject_type"],
                        row["subject_id"],
                        row["relation"],
                        row["object_type"],
                        row["object_id"],
                        row["zone_id"] or "default",
                        now,
                    ),
                )

            # 4. Increment zone revision + commit
            self._increment_zone_revision(zone_id, conn)
            conn.commit()

            # 5. Invalidate graph cache
            self._tuple_version += 1

        return len(rows)
