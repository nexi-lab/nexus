"""Flattened ReBAC Manager — canonical implementation (Issue #1385).

Merges the former ReBACManager + ReBACManager into a single class.

Features:
- P0-1: Version tokens (consistency always cached)
- P0-2: Zone scoping
- P0-5: Graph limits and DoS protection
- Leopard: Pre-computed transitive group closure for O(1) group lookups
- Tiger Cache: Materialized permissions as Roaring Bitmaps

Usage:
    from nexus.bricks.rebac.manager import ReBACManager

    manager = ReBACManager(engine)
"""

import asyncio
import json
import logging
import os
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from nexus.bricks.rebac.batch.bulk_checker import BulkPermissionChecker
from nexus.bricks.rebac.cache.leopard_facade import LeopardFacade
from nexus.bricks.rebac.cache.result_cache import ReBACPermissionCache
from nexus.bricks.rebac.cache.tiger.facade import TigerFacade
from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore
from nexus.bricks.rebac.consistency.revision import (
    get_zone_revision_for_grant,
    increment_version_token,
)
from nexus.bricks.rebac.consistency.zone_manager import ZoneManager
from nexus.bricks.rebac.directory.expander import DirectoryExpander
from nexus.bricks.rebac.domain import (
    RELATION_TO_PERMISSIONS,
    WILDCARD_SUBJECT,
    Entity,
    NamespaceConfig,
)
from nexus.bricks.rebac.graph.bulk_evaluator import (
    check_direct_relation as _check_direct_relation_in_graph,
)
from nexus.bricks.rebac.graph.bulk_evaluator import (
    compute_permission as _compute_permission_bulk,
)
from nexus.bricks.rebac.graph.bulk_evaluator import (
    find_related_objects as _find_related_objects_in_graph,
)
from nexus.bricks.rebac.graph.bulk_evaluator import (
    find_subjects as _find_subjects_in_graph,
)
from nexus.bricks.rebac.graph.expand import ExpandEngine
from nexus.bricks.rebac.graph.traversal import PermissionComputer
from nexus.bricks.rebac.graph.zone_traversal import ZoneAwareTraversal
from nexus.bricks.rebac.path_updater import PathUpdater
from nexus.bricks.rebac.rebac_tracing import (
    record_check_result,
    record_graph_limit_exceeded,
    record_traversal_result,
    start_check_span,
    start_graph_traversal_span,
)
from nexus.bricks.rebac.tuples.repository import TupleRepository
from nexus.bricks.rebac.tuples.writer import TupleWriter
from nexus.bricks.rebac.utils.fast import (
    check_permissions_bulk_with_fallback,
    is_rust_available,
)
from nexus.bricks.rebac.zone_graph_loader import ZoneGraphLoader
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.rebac_types import (
    CROSS_ZONE_ALLOWED_RELATIONS,
    CheckResult,
    GraphLimitExceeded,
    GraphLimits,
    TraversalStats,
    WriteResult,
)
from nexus.lib.zone import normalize_zone_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from nexus.bricks.rebac.cache.leopard import LeopardIndex
    from nexus.bricks.rebac.cache.tiger import TigerCache, TigerCacheUpdater
    from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore

logger = logging.getLogger(__name__)

# ============================================================================
# Flattened ReBAC Manager (Issue #1385)
# ============================================================================


class ReBACManager:
    """Unified ReBAC Manager.

    Provides Zanzibar-style relationship-based access control with:
    - P0-1: Consistency levels and version tokens
    - P0-2: Zone scoping
    - P0-5: Graph limits and DoS protection
    - Leopard: Pre-computed transitive group closure for O(1) group lookups
    - Tiger Cache: Materialized permissions as Roaring Bitmaps
    - Direct tuple lookup + recursive graph traversal
    - Permission expansion via namespace configs
    - Multi-layer caching with TTL and invalidation
    - Cycle detection

    This is the GA-ready ReBAC implementation.
    """

    # Relations that represent group membership
    MEMBERSHIP_RELATIONS = frozenset({"member-of", "member", "belongs-to"})

    def __init__(
        self,
        engine: "Engine",
        cache_ttl_seconds: int = 300,
        max_depth: int = 50,
        enforce_zone_isolation: bool = False,
        enable_graph_limits: bool = True,
        enable_leopard: bool = True,
        enable_tiger_cache: bool = True,
        read_engine: "Engine | None" = None,
        is_postgresql: bool = False,
        version_store: MetastoreVersionStore | None = None,
        namespace_store: "MetastoreNamespaceStore | None" = None,
        enable_inheritance: bool = True,
    ):
        """Initialize ReBAC manager.

        Args:
            engine: SQLAlchemy database engine (primary/write)
            cache_ttl_seconds: Cache TTL in seconds (default: 5 minutes)
            max_depth: Maximum graph traversal depth (default: 50 hops)
            enforce_zone_isolation: Enable zone isolation checks (default: True)
            enable_graph_limits: Enable graph limit enforcement (default: True)
            enable_leopard: Enable Leopard transitive closure index (default: True)
            enable_tiger_cache: Enable Tiger Cache for materialized permissions (default: True)
            read_engine: Optional separate engine for read-only operations (Issue #725).
                        Defaults to ``engine`` when not provided.
            is_postgresql: Whether the database is PostgreSQL (config-time flag).
            version_store: MetastoreVersionStore for zone revision tracking (Issue #191).
            namespace_store: MetastoreNamespaceStore for namespace config (Issue #183).
            enable_inheritance: Enable automatic parent tuple creation in HierarchyManager.
        """
        # ── Base initialization (formerly in ReBACManager.__init__) ──
        self.engine = engine
        self._is_postgresql = is_postgresql
        self._version_store = version_store
        self._namespace_store = namespace_store
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_depth = max_depth
        self._last_cleanup_time: datetime | None = None
        self._namespaces_initialized = False
        self._tuple_version: int = 0

        # Compose TupleRepository for data access delegation (Issue #725: read/write split)
        self._repo = TupleRepository(
            engine,
            read_engine=read_engine,
            is_postgresql=is_postgresql,
            version_store=version_store,
        )

        # Compose graph traversal and expand engines
        self._computer = PermissionComputer(self._repo, self.get_namespace, max_depth)
        self._expander = ExpandEngine(self._repo, self.get_namespace, max_depth)

        # Initialize L1 in-memory cache
        self._l1_cache: ReBACPermissionCache | None = None
        self._l1_cache = ReBACPermissionCache(
            max_size=50000,
            ttl_seconds=cache_ttl_seconds,
            enable_metrics=True,
            enable_adaptive_ttl=False,
            revision_quantization_window=10,
        )
        self._l1_cache.set_revision_fetcher(lambda zone_id: self.get_zone_revision(zone_id))

        # SQLAlchemy sessionmaker for proper connection management
        from sqlalchemy.orm import sessionmaker

        self.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        # ── Enhanced initialization ──
        # Zone isolation (absorbed from ZoneAwareReBACManager — Phase 10)
        self.enforce_zone_isolation = enforce_zone_isolation
        self._zone_manager = ZoneManager(enforce=enforce_zone_isolation)
        self.enable_graph_limits = enable_graph_limits
        self.enable_leopard = enable_leopard
        self.enable_tiger_cache = enable_tiger_cache
        # REMOVED: self._version_counter (replaced with DB sequence in Issue #2 fix)

        # Zone graph cache placeholder — actual loader created after leopard_facade
        # (initialized below, after leopard_facade is created)

        # Leopard index for O(1) transitive group lookups (Issue #692)
        self._leopard: "LeopardIndex | None" = None
        if enable_leopard:
            from nexus.bricks.rebac.cache.leopard import LeopardIndex

            self._leopard = LeopardIndex(
                engine=engine,
                cache_enabled=True,
                cache_max_size=100_000,
                is_postgresql=is_postgresql,
            )

        # Issue #2179: Leopard facade (encapsulates get/rebuild/invalidate/update)
        self._leopard_facade = LeopardFacade(
            leopard=self._leopard,
            engine=engine,
            is_postgresql=is_postgresql,
        )

        # Issue #2179: Zone graph loader (owns zone tuple cache + fetch helpers)
        self._zone_loader = ZoneGraphLoader(
            connection_factory=self._connection,
            create_cursor=self._create_cursor,
            fix_sql=self._fix_sql_placeholders,
            get_namespace_configs_for_rust=self._get_namespace_configs_for_rust,
            leopard_facade=self._leopard_facade,
            cache_ttl=cache_ttl_seconds,
        )
        # Backward compat aliases used by CacheCoordinator
        self._zone_graph_cache = self._zone_loader.raw_cache

        # Tiger Cache for materialized permissions (Issue #682)
        # Only enable on PostgreSQL - SQLite has lock contention issues
        self._tiger_cache: "TigerCache | None" = None
        self._tiger_updater: "TigerCacheUpdater | None" = None
        if enable_tiger_cache and is_postgresql:
            from nexus.bricks.rebac.cache.tiger import (
                TigerCache,
                TigerCacheUpdater,
                TigerResourceMap,
            )

            resource_map = TigerResourceMap(engine, is_postgresql=is_postgresql)
            self._tiger_cache = TigerCache(
                engine=engine,
                resource_map=resource_map,
                rebac_manager=self,
                is_postgresql=is_postgresql,
            )
            self._tiger_updater = TigerCacheUpdater(
                engine=engine,
                tiger_cache=self._tiger_cache,
                rebac_manager=self,
                is_postgresql=is_postgresql,
            )

        # Issue #1459 Phase 12: Tiger Cache facade
        self._tiger_facade = TigerFacade(
            tiger_cache=self._tiger_cache,
            tiger_updater=self._tiger_updater,
        )

        # Issue #2179: Path updater (handles file/directory rename in tuples)
        self._path_updater = PathUpdater(
            connection_factory=self._connection,
            create_cursor=self._create_cursor,
            fix_sql=self._fix_sql_placeholders,
            invalidate_cache_cb=self._invalidate_cache_for_tuple,
            tiger_invalidate_cache_cb=(
                self.tiger_invalidate_cache if hasattr(self, "tiger_invalidate_cache") else None
            ),
            tiger_cache=self._tiger_cache,
        )

        # Issue #2179: Tuple writer (handles write/delete SQL operations)
        self._tuple_writer = TupleWriter(
            connection_factory=self._connection,
            create_cursor=self._create_cursor,
            fix_sql=self._fix_sql_placeholders,
            is_postgresql=is_postgresql,
            repo=self._repo,
            zone_manager=self._zone_manager,
            ensure_namespaces_cb=self._ensure_namespaces_initialized,
            validate_cross_zone_cb=self._validate_cross_zone,
            would_create_cycle_cb=self._would_create_cycle_with_conn,
            increment_zone_revision_cb=self._increment_zone_revision,
            invalidate_cache_cb=self._invalidate_cache_for_tuple,
            get_tuple_version=lambda: self._tuple_version,
            set_tuple_version=lambda v: setattr(self, "_tuple_version", v),
        )

        # Issue #1459 Phase 13: Directory permission expander (Leopard-style)
        self._directory_expander = DirectoryExpander(
            engine=engine,
            tiger_cache=self._tiger_cache,
            is_postgresql=is_postgresql,
        )

        # Issue #1459 Phase 15+: Zone-aware graph traversal
        self._zone_traversal = ZoneAwareTraversal(
            engine=engine,
            get_namespace=self.get_namespace,
            evaluate_conditions=self._evaluate_conditions,
            zone_manager=self._zone_manager,
            enable_graph_limits=enable_graph_limits,
        )

        # Issue #1459 Phase 15+: Bulk permission checker
        self._bulk_checker = BulkPermissionChecker(
            engine=engine,
            get_namespace=self.get_namespace,
            enforce_zone_isolation=enforce_zone_isolation,
            l1_cache=self._l1_cache,
            tiger_cache=self._tiger_cache,
            compute_bulk_helper=self._compute_permission_bulk_helper,
            rebac_check_single=self.rebac_check,
            cache_result=self._cache_check_result,
            tuple_version=getattr(self, "_tuple_version", 0),
            is_postgresql=is_postgresql,
        )

        # Iterator cache for paginated list operations (Issue #722)
        from nexus.bricks.rebac.cache.iterator import IteratorCache

        self._iterator_cache: IteratorCache = IteratorCache(
            max_size=1000,
            ttl_seconds=cache_ttl_seconds,
        )

        # Issue #922: Permission boundary cache for O(1) inheritance checks
        from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache

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
        from nexus.bricks.rebac.cache.coordinator import CacheCoordinator

        self._cache_coordinator: CacheCoordinator = CacheCoordinator(
            l1_cache=self._l1_cache,
            boundary_cache=self._boundary_cache,
            iterator_cache=self._iterator_cache,
            zone_graph_cache=self._zone_graph_cache,
            # Database access (Issue #2179 Step 2.5)
            connection_factory=self._connection,
            get_connection=self._get_connection,
            close_connection=self._close_connection,
            create_cursor=self._create_cursor,
            fix_sql=self._fix_sql_placeholders,
            # Eager recompute callbacks
            get_namespace_cb=self.get_namespace,
            compute_permission_cb=self._compute_permission,
            cache_check_result_cb=self._cache_check_result,
            # Async eager recompute only safe with real connection pools (PostgreSQL).
            # SQLite + StaticPool shares a single DBAPI connection across threads,
            # causing segfaults when the background thread closes it mid-query.
            enable_async_recompute=is_postgresql,
            # Stats / cleanup
            cache_ttl_seconds=self.cache_ttl_seconds,
            get_tuple_version=lambda: self._tuple_version,
            set_tuple_version=lambda v: setattr(self, "_tuple_version", v),
            # Issue #3192: DT_STREAM + Pub/Sub
            invalidation_stream=self._create_invalidation_stream(),
            pubsub=self._create_pubsub(),
        )

        # Issue #3395: Wire Tiger L2 (Dragonfly) invalidation into coordinator.
        # The coordinator expands relation → permissions internally, so the
        # callback receives individual permissions matching Tiger cache keys.
        if self._tiger_cache is not None:
            tc = self._tiger_cache

            def _tiger_l2_cb(st: str, si: str, perm: str, rt: str, zid: str) -> None:
                tc.evict_cached(st, si, perm, rt, zid)

            self._cache_coordinator.register_tiger_l2_invalidator("tiger_bitmap", _tiger_l2_cb)

        # Issue #3192: Wire SharedRingBuffer for cross-process revision broadcasting
        self._wire_shared_ring_buffer()

        # ── Internalized sub-components (formerly factory-constructed) ────
        # These are rebac-internal concerns — factory shouldn't construct them.

        # HierarchyManager: automatic parent tuple creation on permission writes
        from nexus.bricks.rebac.hierarchy_manager import HierarchyManager

        self._hierarchy_manager: HierarchyManager = HierarchyManager(
            rebac_manager=self,
            enable_inheritance=enable_inheritance,
        )

        # DirectoryVisibilityCache: O(1) directory permission lookups via Tiger bitmaps
        from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

        self._dir_visibility_cache: DirectoryVisibilityCache = DirectoryVisibilityCache(
            tiger_cache=self._tiger_cache,
            ttl=cache_ttl_seconds,
            max_entries=10000,
        )
        # Wire invalidation: permission changes → dir visibility cache
        self.register_dir_visibility_invalidator(
            "nexusfs",
            lambda zone_id, path: self._dir_visibility_cache.invalidate_for_resource(path, zone_id),
        )

        # NamespaceManager: optional (profile-gated), created on demand via
        # create_namespace_manager() when namespace brick is enabled.
        self._namespace_manager: "Any | None" = None

    # ── Public property accessors for internalized sub-components ────

    @property
    def hierarchy_manager(self) -> Any:
        """HierarchyManager — automatic parent tuple creation on permission writes."""
        return self._hierarchy_manager

    @property
    def dir_visibility_cache(self) -> Any:
        """DirectoryVisibilityCache — O(1) directory permission lookups."""
        return self._dir_visibility_cache

    @property
    def namespace_manager(self) -> Any:
        """NamespaceManager — per-subject namespace visibility (may be None if not created)."""
        return self._namespace_manager

    def create_namespace_manager(self, record_store: Any = None) -> Any:
        """Create and attach the NamespaceManager (profile-gated, call when namespace brick is enabled).

        Args:
            record_store: RecordStoreABC for L3 persistent view store. If None, L3 is disabled.

        Returns:
            The newly created NamespaceManager instance.
        """
        from nexus.bricks.rebac.namespace_factory import (
            create_namespace_manager as _create_ns_manager,
        )

        self._namespace_manager = _create_ns_manager(self, record_store)
        return self._namespace_manager

    def _create_invalidation_stream(self) -> Any:
        """Create the DT_STREAM for ordered intra-zone invalidation."""
        from nexus.bricks.rebac.cache.invalidation_stream import InvalidationStream

        return InvalidationStream()

    def _create_pubsub(self) -> Any:
        """Create the Pub/Sub for cross-zone invalidation hints."""
        from nexus.bricks.rebac.cache.pubsub_invalidation import PubSubInvalidation

        return PubSubInvalidation()

    def _wire_shared_ring_buffer(self) -> None:
        """Create and inject SharedRingBuffers for cross-process IPC (Issue #3192).

        Uses separate buffers for each message type to avoid multiplexing
        incompatible schemas. Each buffer is SPSC: one producer per process,
        one consumer per process. Multi-process safety is achieved by using
        unique buffer names per process (PID suffix).
        """
        try:
            import os

            from nexus.bricks.rebac.cache.shared_ring_buffer import SharedRingBuffer

            pid = os.getpid()

            # Separate buffer for zone graph tuple notifications
            if self._zone_loader is not None:
                zone_ring = SharedRingBuffer(
                    name=f"rebac-zone-tuples-{pid}",
                    entry_size=256,
                    capacity=1024,
                ).open_producer()
                self._zone_loader.set_ring_buffer(zone_ring)

            # Separate buffer for revision sequence broadcasting
            if self._l1_cache is not None:
                rev_ring = SharedRingBuffer(
                    name=f"rebac-revisions-{pid}",
                    entry_size=128,
                    capacity=1024,
                ).open_producer()
                self._l1_cache.set_revision_ring_buffer(rev_ring)

            logger.info("[RING-BUFFER] SharedRingBuffers wired (pid=%d)", pid)
        except Exception as e:
            logger.debug("[RING-BUFFER] SharedRingBuffer not available: %s", e)

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,  # Issue #773: Defaults to "root" internally
    ) -> bool:
        """Check permission (always uses cached/eventual consistency).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation
            zone_id: Zone ID to scope check

        Returns:
            True if permission is granted, False otherwise

        Raises:
            GraphLimitExceeded: If graph traversal exceeds limits (P0-5)

        Example:
            result = manager.rebac_check(subject, permission, object)
        """
        logger.debug(
            f"ReBACManager.rebac_check called: enforce_zone_isolation={self.enforce_zone_isolation}, MAX_DEPTH={GraphLimits.MAX_DEPTH}"
        )

        # Issue #702: OTel tracing — wrap the entire check in a root span
        check_start = time.perf_counter()
        with start_check_span(
            subject=subject,
            permission=permission,
            obj=object,
            zone_id=zone_id,
            consistency="cached",
        ) as _check_span:
            result = self._rebac_check_inner(
                subject,
                permission,
                object,
                context,
                zone_id,
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
            and context is None
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
                    if logger.isEnabledFor(logging.DEBUG):
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
        if self._tiger_cache and zone_id and context is None:
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

        # If zone isolation is disabled, use base (non-zone-aware) check path
        if not self.enforce_zone_isolation:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"  -> Falling back to base check path, max_depth={self.max_depth}")
            result = self._rebac_check_base(subject, permission, object, context, zone_id)

            # Write-through to Tiger Cache (Issue #935)
            if result and self._tiger_cache and zone_id and context is None:
                self._tiger_write_through_single(subject, permission, object, zone_id, logger)

            # Issue #922: Cache boundary if permission was granted via parent
            if result and object_type == "file" and self._boundary_cache and context is None:
                self._cache_boundary_if_inherited(subject, permission, object, zone_id, logger)

            return result

        logger.debug("  -> Using rebac_check_detailed")
        detailed_result = self.rebac_check_detailed(subject, permission, object, context, zone_id)
        logger.debug(
            f"  -> rebac_check_detailed result: allowed={detailed_result.allowed}, indeterminate={detailed_result.indeterminate}"
        )

        # Write-through to Tiger Cache (Issue #935)
        if detailed_result.allowed and self._tiger_cache and zone_id and context is None:
            self._tiger_write_through_single(subject, permission, object, zone_id, logger)

        # Issue #922: Cache boundary if permission was granted via parent
        if (
            detailed_result.allowed
            and object_type == "file"
            and self._boundary_cache
            and context is None
        ):
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
        effective_zone = normalize_zone_id(zone_id)
        subject_type, subject_id = subject
        object_type, object_id = object

        with self._connection(readonly=True) as conn:
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
        zone_id: str | None = None,  # Issue #773: Defaults to "root" internally
    ) -> CheckResult:
        """Check permission with detailed result metadata.

        Always uses cached (eventual) consistency.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            object: (object_type, object_id) tuple
            context: Optional ABAC context for condition evaluation
            zone_id: Zone ID to scope check

        Returns:
            CheckResult with consistency metadata and traversal stats
        """
        # BUGFIX (Issue #3): Fail fast on missing zone_id in production
        # In production, missing zone_id is a security issue - reject immediately
        if not zone_id:
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
                logger.warning(
                    f"rebac_check called without zone_id, defaulting to 'root'. "
                    f"This is only allowed in development. Stack:\n{''.join(traceback.format_stack()[-5:])}"
                )
            zone_id = ROOT_ZONE_ID

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        # BUGFIX (Issue #4): Use perf_counter for elapsed time measurement
        # time.time() uses wall clock which can jump (NTP, DST), causing incorrect timeouts
        # perf_counter() is monotonic and immune to clock adjustments
        start_time = time.perf_counter()

        # Clean up expired tuples
        self._cleanup_expired_tuples_if_needed()

        # Always use cached (eventual) consistency: Use cache (up to cache_ttl_seconds staleness)
        cached = self._get_cached_check_zone_aware(
            subject_entity, permission, object_entity, zone_id
        )
        if cached is not None:
            if logger.isEnabledFor(logging.DEBUG):
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
            from nexus.bricks.rebac.utils.fast import (
                check_permission_single_rust,
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
        self,
        zone_id: str,
        subject: Entity | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch tuples for Rust permission computation. Delegates to ZoneGraphLoader."""
        return self._zone_loader.fetch_tuples_for_rust(zone_id, subject)

    def get_zone_tuples(self, zone_id: str) -> list[dict[str, Any]]:
        """Fetch all permission tuples for a zone. Delegates to ZoneGraphLoader."""
        return self._zone_loader.get_zone_tuples(zone_id)

    def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None:
        """Invalidate the zone graph cache. Delegates to ZoneGraphLoader."""
        self._zone_loader.invalidate(zone_id)

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
        permissions = RELATION_TO_PERMISSIONS.get(relation, [])
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

    def register_namespace_invalidator(
        self,
        callback_id: str,
        callback: Any,
    ) -> None:
        """Register a namespace cache invalidation callback (Issue #1244).

        Delegates to CacheCoordinator. Called on every rebac_write/rebac_delete
        to immediately invalidate the affected subject's dcache entries.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(subject_type, subject_id, zone_id)
        """
        self._cache_coordinator.register_namespace_invalidator(callback_id, callback)

    def unregister_namespace_invalidator(self, callback_id: str) -> bool:
        """Unregister a namespace cache invalidation callback.

        Args:
            callback_id: ID of callback to remove

        Returns:
            True if callback was found and removed, False otherwise
        """
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

    def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: datetime | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,  # Issue #773: Defaults to "root" internally
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

        Example:
            result = manager.rebac_write(subject, relation, object, zone_id=zone)
            allowed = manager.rebac_check(subject, permission, object)
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
        if relation in self.MEMBERSHIP_RELATIONS:
            self._leopard_facade.on_membership_add(
                subject[0],
                subject[1],
                object[0],
                object[1],
                effective_zone,
            )

        # Tiger Cache: Write-through - persist grant immediately
        # This is the fast path (~1-5ms) vs queue processing (~20-40s)
        if self._tiger_cache and not conditions:
            subject_tuple = (subject[0], subject[1])
            object_type = object[0]
            object_id = object[1]

            # Get permissions for this relation (fail-closed: unknown → [])
            permissions = RELATION_TO_PERMISSIONS.get(relation, [])

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
        # Call base batch write implementation
        created_count = self._rebac_write_batch_base(tuples)

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
            for t in tuples:
                relation = t.get("relation")
                if relation in self.MEMBERSHIP_RELATIONS:
                    subject = t["subject"]
                    obj = t["object"]
                    zone_id = normalize_zone_id(t.get("zone_id"))
                    self._leopard_facade.on_membership_add(
                        subject[0],
                        subject[1],
                        obj[0],
                        obj[1],
                        zone_id,
                    )

            # Tiger Cache: Write-through for bulk operations
            if self._tiger_cache:
                for t in tuples:
                    if t.get("conditions"):
                        continue

                    subject = t["subject"]
                    obj = t["object"]
                    relation = t.get("relation", "")
                    zone_id = normalize_zone_id(t.get("zone_id"))
                    subject_tuple = (subject[0], subject[1])
                    object_type = obj[0]
                    object_id = obj[1]

                    # Get permissions for this relation
                    # FIX: Default to empty list for unknown relations
                    permissions = RELATION_TO_PERMISSIONS.get(relation, [])

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
        """Insert a relationship tuple with zone isolation. Delegates to TupleWriter."""
        return self._tuple_writer.write_tuple_zone_aware(
            subject=subject,
            relation=relation,
            object=object,
            enforce_zone_isolation=self.enforce_zone_isolation,
            expires_at=expires_at,
            conditions=conditions,
            zone_id=zone_id,
            subject_zone_id=subject_zone_id,
            object_zone_id=object_zone_id,
        )

    def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str = ROOT_ZONE_ID,
    ) -> list[tuple[str, str]]:
        """Find all subjects with permission on object (zone-scoped).

        Args:
            permission: Permission to check
            object: (object_type, object_id) tuple
            zone_id: Zone ID to scope expansion

        Returns:
            List of (subject_type, subject_id) tuples within zone
        """
        # If zone isolation is disabled, use base expand implementation
        if not self.enforce_zone_isolation:
            return self._expander.expand(permission, object)

        if not zone_id:
            zone_id = ROOT_ZONE_ID

        object_entity = Entity(object[0], object[1])
        subjects: set[tuple[str, str]] = set()

        # Get namespace config
        namespace = self.get_namespace(object_entity.entity_type)
        if not namespace:
            return self._get_direct_subjects_zone_aware(permission, object_entity, zone_id)

        # Resolve permission → relation mapping (e.g. "write" → ["editor", "owner"])
        # Permissions are defined in config["permissions"], relations in config["relations"]
        perm_relations = namespace.config.get("permissions", {}).get(permission)
        if perm_relations:
            # Permission name maps to one or more relations — expand each
            for rel in perm_relations:
                self._expand_permission_zone_aware(
                    rel, object_entity, namespace, zone_id, subjects, visited=set(), depth=0
                )
        else:
            # Already a relation name (or unknown) — expand directly
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
        with self._connection(readonly=True) as conn:
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
        """Get cached permission check result (zone-aware cache key).

        Note: L2 SQL cache (rebac_check_cache) removed — always returns None (cache miss).
        L1 in-memory cache is checked by callers before this method.
        """
        return None

    def _cache_check_result_zone_aware(
        self, subject: Entity, permission: str, obj: Entity, zone_id: str, result: bool
    ) -> None:
        """Cache permission check result (zone-aware cache key).

        Note: L2 SQL cache (rebac_check_cache) removed — no-op.
        L1 in-memory caching is handled by callers.
        """

    # ============================================================================
    # End Zone-Aware Methods
    # ============================================================================

    def rebac_delete(self, tuple_id: str | WriteResult) -> bool:
        """Delete a relationship tuple with cache invalidation.

        Overrides parent to invalidate the zone graph cache after deletes.

        Args:
            tuple_id: ID of tuple to delete (str or WriteResult from rebac_write)

        Returns:
            True if tuple was deleted, False if not found
        """
        # Accept WriteResult for convenience (rebac_write returns WriteResult)
        if isinstance(tuple_id, WriteResult):
            tuple_id = tuple_id.tuple_id

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

        # Call base delete implementation
        result = self._rebac_delete_base(tuple_id)

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
                    permissions = RELATION_TO_PERMISSIONS.get(relation, [])

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
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug(f"[TIGER] Revoke failed: {e}")

            # Leopard: Update transitive closure for membership relations
            if tuple_info["relation"] in self.MEMBERSHIP_RELATIONS:
                effective_zone = normalize_zone_id(zone_id)
                self._leopard_facade.on_membership_remove(
                    tuple_info["subject_type"],
                    tuple_info["subject_id"],
                    tuple_info["object_type"],
                    tuple_info["object_id"],
                    effective_zone,
                )

            # Boundary Cache: Invalidate cached boundaries for affected subject+object
            if self._boundary_cache:
                effective_zone_bc = normalize_zone_id(zone_id)
                for perm in RELATION_TO_PERMISSIONS.get(tuple_info["relation"], []):
                    self._boundary_cache.invalidate_permission_change(
                        effective_zone_bc,
                        tuple_info["subject_type"],
                        tuple_info["subject_id"],
                        perm,
                        tuple_info["object_id"],
                    )

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

    def rebac_delete_by_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str | None = None,
    ) -> int:
        """Delete all ReBAC tuples for a given subject. Delegates to TupleWriter."""
        return self._tuple_writer.delete_by_subject(
            subject_type=subject_type,
            subject_id=subject_id,
            zone_id=zone_id,
        )

    # ========================================================================
    # Leopard Index Methods (Issue #692)
    # ========================================================================

    def get_transitive_groups(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> set[tuple[str, str]]:
        """Get all groups a subject transitively belongs to. Delegates to LeopardFacade."""
        return self._leopard_facade.get_transitive_groups(subject_type, subject_id, zone_id)

    def rebuild_leopard_closure(self, zone_id: str) -> int:
        """Rebuild the Leopard transitive closure for a zone. Delegates to LeopardFacade."""
        return self._leopard_facade.rebuild_closure(zone_id)

    def invalidate_leopard_cache(self, zone_id: str | None = None) -> None:
        """Invalidate Leopard in-memory cache. Delegates to LeopardFacade."""
        self._leopard_facade.invalidate_cache(zone_id)

    # ========================================================================
    # Tiger Cache Methods (Issue #682)
    # ========================================================================

    def _tiger_write_through_single(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str,
        _logger: Any = None,
    ) -> None:
        """Write-through single permission result to Tiger Cache. Delegates to TupleWriter."""
        self._tuple_writer.tiger_write_through_single(
            subject=subject,
            permission=permission,
            object=object,
            zone_id=zone_id,
            tiger_cache=self._tiger_cache,
        )

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
        effective_zone = zone_id or ROOT_ZONE_ID
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
        if self._version_store is not None:
            return get_zone_revision_for_grant(self._version_store, zone_id)
        return 0

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

    def _get_version_token(self, zone_id: str = ROOT_ZONE_ID) -> str:
        """Get current version token (P0-1).

        Delegates to consistency.revision module (Issue #1459).
        """
        if self._version_store is not None:
            return increment_version_token(self._version_store, zone_id)
        return "v0"

    def _get_cached_check_zone_aware_bounded(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        zone_id: str,
        max_age_seconds: float,
    ) -> bool | None:
        """Get cached result with bounded staleness (P0-1).

        Note: L2 SQL cache (rebac_check_cache) removed — always returns None (cache miss).
        """
        return None

    def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
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
        return self._bulk_checker.check_bulk(checks, zone_id)

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
        from nexus.bricks.rebac.utils.fast import (
            RUST_AVAILABLE,
            list_objects_for_subject_rust,
        )

        start_time = time.perf_counter()

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
        if logger.isEnabledFor(logging.DEBUG):
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
                elapsed = (time.perf_counter() - start_time) * 1000
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

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: list[str] | None = None,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """List relationship tuples matching optional filters.

        Protocol-compliant method for querying tuples by criteria.
        Used by composition-layer code (e.g., zone_helpers) that needs
        to find tuple IDs for targeted deletion.

        Args:
            subject: Optional (type, id) filter.
            relation: Optional single relation filter.
            object: Optional (type, id) filter.
            relation_in: Optional list of relations to match.

        Returns:
            List of tuple dicts with keys: tuple_id, subject_type,
            subject_id, relation, object_type, object_id, zone_id.
        """
        fix = self._fix_sql_placeholders
        clauses: list[str] = []
        params: list[Any] = []

        if subject is not None:
            clauses.append("subject_type = ? AND subject_id = ?")
            params.extend(subject)
        if relation is not None:
            clauses.append("relation = ?")
            params.append(relation)
        elif relation_in:
            placeholders = ", ".join("?" for _ in relation_in)
            clauses.append(f"relation IN ({placeholders})")
            params.extend(relation_in)
        if object is not None:
            clauses.append("object_type = ? AND object_id = ?")
            params.extend(object)

        where = " AND ".join(clauses) if clauses else "1=1"
        sql = fix(
            f"SELECT tuple_id, subject_type, subject_id, relation, "
            f"object_type, object_id, zone_id "
            f"FROM rebac_tuples WHERE {where}"
        )

        with self._connection() as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(sql, params)
            return [
                {
                    "tuple_id": row["tuple_id"],
                    "subject_type": row["subject_type"],
                    "subject_id": row["subject_id"],
                    "relation": row["relation"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "zone_id": row.get("zone_id") if hasattr(row, "get") else row["zone_id"],
                }
                for row in cursor.fetchall()
            ]

    def get_cross_zone_shared_paths(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
        object_type: str = "file",
        prefix: str = "",
    ) -> list[str]:
        """Return distinct object paths shared with a subject from other zones."""
        cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
        placeholders = ", ".join("?" for _ in cross_zone_relations)
        query = f"""
            SELECT DISTINCT object_id
            FROM rebac_tuples
            WHERE relation IN ({placeholders})
              AND subject_type = ? AND subject_id = ?
              AND object_type = ?
              AND zone_id != ?
              AND (expires_at IS NULL OR expires_at > ?)
        """
        params: tuple[Any, ...] = (
            *cross_zone_relations,
            subject_type,
            subject_id,
            object_type,
            zone_id,
            datetime.now(UTC).isoformat(),
        )
        if prefix:
            query += " AND object_id LIKE ?"
            params = (*params, f"{prefix}%")

        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(self._fix_sql_placeholders(query), params)
            paths: list[str] = []
            for row in cursor.fetchall():
                path = row["object_id"] if hasattr(row, "keys") else row[0]
                paths.append(path)
            return paths

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
        start_time = time.perf_counter()

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

        elapsed = (time.perf_counter() - start_time) * 1000
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

    # ====================================================================================
    # Connection Management
    # ====================================================================================

    def _get_connection(self) -> Any:
        """Get a DBAPI connection from the pool.

        Delegates to TupleRepository (Issue #1459).
        """
        return self._repo.get_connection()

    def _close_connection(self, conn: Any) -> None:
        """Close a connection obtained from _get_connection().

        Delegates to TupleRepository (Issue #1459).
        """
        self._repo.close_connection(conn)

    @property
    def supports_old_new_returning(self) -> bool:
        """Check if database supports OLD/NEW in RETURNING clauses.

        Delegates to TupleRepository (Issue #1459).
        """
        return self._repo.supports_old_new_returning

    def get_zone_revision(self, zone_id: str | None, conn: Any | None = None) -> int:
        """Get current revision for a zone. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.get_zone_revision(zone_id, conn)

    def list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        relation_in: list[str] | None = None,
        object: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """List ReBAC tuples with optional filters.

        Args:
            subject: (subject_type, subject_id) filter
            relation: Single relation filter
            relation_in: Multiple relation filter (mutually exclusive with relation)
            object: (object_type, object_id) filter

        Returns:
            List of tuple dicts.
        """
        conn = self._get_connection()
        try:
            query = "SELECT * FROM rebac_tuples WHERE 1=1"
            params: list[Any] = []

            if subject:
                query += " AND subject_type = ? AND subject_id = ?"
                params.extend([subject[0], subject[1]])

            if relation:
                query += " AND relation = ?"
                params.append(relation)
            elif relation_in:
                placeholders = ", ".join("?" * len(relation_in))
                query += f" AND relation IN ({placeholders})"
                params.extend(relation_in)

            if object:
                query += " AND object_type = ? AND object_id = ?"
                params.extend([object[0], object[1]])

            query = self._fix_sql_placeholders(query)
            cursor = self._create_cursor(conn)
            cursor.execute(query, params)

            def _safe_get(row: Any, key: str) -> Any:
                """Read a nullable column from any row type (dict, sqlite3.Row, etc.)."""
                try:
                    return row[key]
                except (KeyError, IndexError):
                    return None

            results = []
            for row in cursor.fetchall():
                results.append(
                    {
                        "tuple_id": row["tuple_id"],
                        "subject_type": row["subject_type"],
                        "subject_id": row["subject_id"],
                        "subject_relation": _safe_get(row, "subject_relation"),
                        "relation": row["relation"],
                        "object_type": row["object_type"],
                        "object_id": row["object_id"],
                        "created_at": row["created_at"],
                        "expires_at": _safe_get(row, "expires_at"),
                        "conditions": _safe_get(row, "conditions"),
                        "zone_id": _safe_get(row, "zone_id"),
                        "subject_zone_id": _safe_get(row, "subject_zone_id"),
                        "object_zone_id": _safe_get(row, "object_zone_id"),
                    }
                )

            return results
        finally:
            self._close_connection(conn)

    def _increment_zone_revision(self, zone_id: str | None, conn: Any) -> int:
        """Increment and return the new revision. Delegates to TupleRepository (Issue #1459)."""
        new_rev = self._repo.increment_zone_revision(zone_id, conn)

        # Issue #3192: Broadcast revision via SharedRingBuffer
        if self._l1_cache is not None:
            ring = getattr(self._l1_cache, "_revision_ring_buffer", None)
            if ring is not None:
                try:
                    import json

                    ring.write(
                        json.dumps(
                            {"zone_id": zone_id or ROOT_ZONE_ID, "revision": new_rev}
                        ).encode()
                    )
                except Exception:
                    pass  # best-effort broadcast

        return new_rev

    @contextmanager
    def _connection(self, *, readonly: bool = False) -> Any:
        """Context manager for database connections. Delegates to TupleRepository (Issue #1459).

        Args:
            readonly: If True, uses the read engine and skips commit (Issue #725).
                     Use for pure SELECT operations (permission checks, cache lookups).
        """
        with self._repo.connection(readonly=readonly) as conn:
            yield conn

    def _create_cursor(self, conn: Any) -> Any:
        """Create a cursor with appropriate cursor factory. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.create_cursor(conn)

    # ====================================================================================
    # Namespace Management
    # ====================================================================================

    def _ensure_namespaces_initialized(self) -> None:
        """Ensure default namespaces are initialized (called before first ReBAC operation)."""
        if not self._namespaces_initialized:
            logger.info("Initializing default namespaces...")

            # Use engine.connect() to leverage pool_pre_ping for stale connection detection
            with self.engine.connect() as sa_conn:
                try:
                    dbapi_conn = sa_conn.connection.dbapi_connection
                    self._initialize_default_namespaces_with_conn(dbapi_conn)
                    sa_conn.commit()
                    self._namespaces_initialized = True
                    logger.info("Default namespaces initialized successfully")
                except Exception as e:  # fail-safe: namespace init is best-effort at startup
                    sa_conn.rollback()
                    logger.warning(f"Failed to initialize namespaces: {type(e).__name__}: {e}")
                    logger.debug(traceback.format_exc())

    def _fix_sql_placeholders(self, sql: str) -> str:
        """Convert SQLite ? placeholders to PostgreSQL %s. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.fix_sql_placeholders(sql)

    def _would_create_cycle_with_conn(
        self, conn: Any, subject: Entity, object_entity: Entity, zone_id: str | None
    ) -> bool:
        """Check if creating a parent relation would create a cycle.

        Delegates to TupleRepository (Issue #1459).
        """
        return self._repo.would_create_cycle(conn, subject, object_entity, zone_id)

    def _initialize_default_namespaces_with_conn(self, conn: Any) -> None:
        """Initialize default namespace configurations with given connection."""
        from nexus.bricks.rebac.default_namespaces import (
            DEFAULT_FILE_NAMESPACE,
            DEFAULT_GROUP_NAMESPACE,
            DEFAULT_MEMORY_NAMESPACE,
            DEFAULT_PLAYBOOK_NAMESPACE,
            DEFAULT_SKILL_NAMESPACE,
            DEFAULT_TRAJECTORY_NAMESPACE,
        )

        all_defaults = [
            DEFAULT_FILE_NAMESPACE,
            DEFAULT_GROUP_NAMESPACE,
            DEFAULT_MEMORY_NAMESPACE,
            DEFAULT_PLAYBOOK_NAMESPACE,
            DEFAULT_TRAJECTORY_NAMESPACE,
            DEFAULT_SKILL_NAMESPACE,
        ]

        # Prefer metastore-backed namespace store (Issue #183)
        if self._namespace_store is not None:
            try:
                for ns_config in all_defaults:
                    self._namespace_store.create_or_update_default(ns_config)
                return
            except Exception as e:
                logger.warning(
                    f"Failed to register default namespaces via metastore: {type(e).__name__}: {e}"
                )
                logger.debug(traceback.format_exc())
                return

        try:
            cursor = self._create_cursor(conn)

            # Check if rebac_namespaces table exists
            if not self._is_postgresql:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='rebac_namespaces'"
                )
            else:  # PostgreSQL
                cursor.execute("SELECT tablename FROM pg_tables WHERE tablename='rebac_namespaces'")

            if not cursor.fetchone():
                return  # Table doesn't exist yet

            # Check and create/update namespaces
            for ns_config in all_defaults:
                cursor.execute(
                    self._fix_sql_placeholders(
                        "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
                    ),
                    (ns_config.object_type,),
                )
                existing = cursor.fetchone()
                if not existing:
                    # Create namespace
                    cursor.execute(
                        self._fix_sql_placeholders(
                            "INSERT INTO rebac_namespaces (namespace_id, object_type, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
                        ),
                        (
                            ns_config.namespace_id,
                            ns_config.object_type,
                            json.dumps(ns_config.config),
                            datetime.now(UTC),
                            datetime.now(UTC),
                        ),
                    )
                else:
                    # BUGFIX for issue #338: Update existing namespace ONLY if it matches our default namespace_id
                    # This prevents overwriting custom namespaces created by tests or users
                    existing_namespace_id = existing["namespace_id"]
                    if existing_namespace_id == ns_config.namespace_id:
                        # This is our default namespace, update it to pick up config changes
                        cursor.execute(
                            self._fix_sql_placeholders(
                                "UPDATE rebac_namespaces SET config = ?, updated_at = ? WHERE namespace_id = ?"
                            ),
                            (
                                json.dumps(ns_config.config),
                                datetime.now(UTC),
                                ns_config.namespace_id,
                            ),
                        )
            conn.commit()
        except Exception as e:  # fail-safe: tables may not exist yet at startup
            logger.warning(f"Failed to register default namespaces: {type(e).__name__}: {e}")
            logger.debug(traceback.format_exc())

    def _initialize_default_namespaces(self) -> None:
        """Initialize default namespace configurations if not present."""
        with self._connection() as conn:
            self._initialize_default_namespaces_with_conn(conn)

    def create_namespace(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace configuration.

        Args:
            namespace: Namespace configuration to create
        """
        # Prefer metastore-backed namespace store (Issue #183)
        if self._namespace_store is not None:
            self._namespace_store.create_or_update(namespace)
            self._invalidate_cache_for_namespace(namespace.object_type)
            return

        with self._connection() as conn:
            cursor = self._create_cursor(conn)

            # Check if namespace exists
            cursor.execute(
                self._fix_sql_placeholders(
                    "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
                ),
                (namespace.object_type,),
            )
            existing = cursor.fetchone()

            if existing:
                # Update existing namespace
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        UPDATE rebac_namespaces
                        SET config = ?, updated_at = ?
                        WHERE object_type = ?
                        """
                    ),
                    (
                        json.dumps(namespace.config),
                        datetime.now(UTC).isoformat(),
                        namespace.object_type,
                    ),
                )
            else:
                # Insert new namespace
                cursor.execute(
                    self._fix_sql_placeholders(
                        """
                        INSERT INTO rebac_namespaces (namespace_id, object_type, config, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        namespace.namespace_id,
                        namespace.object_type,
                        json.dumps(namespace.config),
                        namespace.created_at.isoformat(),
                        namespace.updated_at.isoformat(),
                    ),
                )

            conn.commit()

            # BUGFIX: Invalidate all cached checks for this namespace
            # When namespace config changes, cached permission checks may be stale
            self._invalidate_cache_for_namespace(namespace.object_type)

    def get_namespace(self, object_type: str) -> NamespaceConfig | None:
        """Get namespace configuration for an object type.

        Args:
            object_type: Type of object

        Returns:
            NamespaceConfig or None if not found
        """
        # Prefer metastore-backed namespace store (Issue #183)
        if self._namespace_store is not None:
            data = self._namespace_store.get(object_type)
            if data is None:
                return None
            created_at = data.get("created_at", "")
            updated_at = data.get("updated_at", "")
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at) if created_at else datetime.now(UTC)
            if isinstance(updated_at, str):
                updated_at = datetime.fromisoformat(updated_at) if updated_at else datetime.now(UTC)
            return NamespaceConfig(
                namespace_id=data["namespace_id"],
                object_type=data["object_type"],
                config=data["config"],
                created_at=created_at,
                updated_at=updated_at,
            )

        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT namespace_id, object_type, config, created_at, updated_at
                    FROM rebac_namespaces
                    WHERE object_type = ?
                    """
                ),
                (object_type,),
            )

            row = cursor.fetchone()
            if not row:
                return None

            # Both SQLite and PostgreSQL now return dict-like rows
            created_at = row["created_at"]
            updated_at = row["updated_at"]
            # SQLite returns ISO strings, PostgreSQL returns datetime objects
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            if isinstance(updated_at, str):
                updated_at = datetime.fromisoformat(updated_at)

            return NamespaceConfig(
                namespace_id=row["namespace_id"],
                object_type=row["object_type"],
                config=json.loads(row["config"])
                if isinstance(row["config"], str)
                else row["config"],
                created_at=created_at,
                updated_at=updated_at,
            )

    def list_namespaces(self) -> list[dict[str, Any]]:
        """List all namespace configurations.

        Returns:
            List of namespace dicts with keys: namespace_id, object_type,
            config (parsed JSON), created_at, updated_at.
        """
        # Prefer metastore-backed namespace store (Issue #183)
        if self._namespace_store is not None:
            return self._namespace_store.list_all()

        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(
                self._fix_sql_placeholders(
                    "SELECT namespace_id, object_type, config, created_at, updated_at "
                    "FROM rebac_namespaces ORDER BY object_type"
                )
            )
            return [
                {
                    "namespace_id": row["namespace_id"],
                    "object_type": row["object_type"],
                    "config": json.loads(row["config"])
                    if isinstance(row["config"], str)
                    else row["config"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in cursor.fetchall()
            ]

    def delete_namespace(self, object_type: str) -> bool:
        """Delete a namespace configuration.

        Args:
            object_type: Type of objects to remove namespace for.

        Returns:
            True if namespace was deleted, False if not found.
        """
        # Prefer metastore-backed namespace store (Issue #183)
        if self._namespace_store is not None:
            deleted = self._namespace_store.delete(object_type)
            if deleted:
                cache = getattr(self, "_cache", None)
                if cache is not None:
                    cache.clear()
            return deleted

        conn = self._get_connection()
        try:
            cursor = self._create_cursor(conn)

            cursor.execute(
                self._fix_sql_placeholders(
                    "SELECT namespace_id FROM rebac_namespaces WHERE object_type = ?"
                ),
                (object_type,),
            )
            if cursor.fetchone() is None:
                return False

            cursor.execute(
                self._fix_sql_placeholders("DELETE FROM rebac_namespaces WHERE object_type = ?"),
                (object_type,),
            )
            conn.commit()

            # Invalidate cache if available
            cache = getattr(self, "_cache", None)
            if cache is not None:
                cache.clear()

            return True
        finally:
            self._close_connection(conn)

    def get_tuple_conditions(self, tuple_id: str) -> dict[str, Any] | None:
        """Get the conditions JSON for a specific tuple.

        Args:
            tuple_id: The tuple UUID.

        Returns:
            Parsed conditions dict, or None if not found / no conditions.
        """

        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(
                self._fix_sql_placeholders(
                    "SELECT conditions FROM rebac_tuples WHERE tuple_id = ?"
                ),
                (tuple_id,),
            )
            row = cursor.fetchone()
            if row and row["conditions"]:
                result: dict[str, Any] = json.loads(row["conditions"])
                return result
            return None

    # ====================================================================================
    # Cross-zone Validation
    # ====================================================================================

    def _validate_cross_zone(
        self,
        zone_id: str | None,
        subject_zone_id: str | None,
        object_zone_id: str | None,
    ) -> None:
        """Validate cross-zone relationships. Delegates to TupleRepository (Issue #1459)."""
        TupleRepository.validate_cross_zone(zone_id, subject_zone_id, object_zone_id)

    # ====================================================================================
    # Write/Delete — thin delegates to TupleWriter (Issue #2179 Step 2.4)
    # ====================================================================================

    def _rebac_write_base(
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
        """Base tuple write — no zone-aware wrapping. Delegates to TupleWriter."""
        return self._tuple_writer.write_base(
            subject=subject,
            relation=relation,
            object=object,
            expires_at=expires_at,
            conditions=conditions,
            zone_id=zone_id,
            subject_zone_id=subject_zone_id,
            object_zone_id=object_zone_id,
        )

    def _rebac_write_batch_base(
        self,
        tuples: list[dict[str, Any]],
    ) -> int:
        """Batch tuple write. Delegates to TupleWriter."""
        return self._tuple_writer.write_batch(tuples, l1_cache=self._l1_cache)

    def _bulk_check_tuples_exist(
        self,
        cursor: Any,
        parsed_tuples: list[dict[str, Any]],
    ) -> set[tuple]:
        """Check which tuples already exist. Delegates to TupleRepository (Issue #1459)."""
        return self._repo.bulk_check_tuples_exist(cursor, parsed_tuples)

    def _rebac_delete_base(self, tuple_id: str) -> bool:
        """Delete a relationship tuple. Delegates to TupleWriter."""
        return self._tuple_writer.delete_base(tuple_id)

    # ====================================================================================
    # Update Object Path
    # ====================================================================================

    def update_object_path(
        self,
        old_path: str,
        new_path: str,
        object_type: str = "file",
        is_directory: bool = False,
    ) -> int:
        """Update paths in ReBAC tuples on rename/move. Delegates to PathUpdater."""
        updated_count, should_bump = self._path_updater.update_object_path(
            old_path,
            new_path,
            object_type,
            is_directory,
        )
        if should_bump:
            self._tuple_version += 1
        return updated_count

    # ====================================================================================
    # Check (Renamed from rebac_check)
    # ====================================================================================

    def _rebac_check_base(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object.

        Uses caching and recursive graph traversal to compute permissions.
        Supports ABAC-style contextual conditions (time, location, device, etc.).

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            context: Optional context for ABAC evaluation (time, ip, device, etc.)
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            True if permission is granted, False otherwise

        Example:
            >>> # Basic check
            >>> manager.rebac_check(
            ...     subject=("agent", "alice_id"),
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            True

            >>> # With ABAC context
            >>> manager.rebac_check(
            ...     subject=("agent", "contractor"),
            ...     permission="read",
            ...     object=("file", "sensitive"),
            ...     context={"time": "14:30", "ip": "10.0.1.5"}
            ... )
            True
        """
        logger = logging.getLogger(__name__)

        # Ensure default namespaces are initialized
        self._ensure_namespaces_initialized()

        # Issue #773: Default zone_id to "root" if not provided
        if zone_id is None:
            zone_id = ROOT_ZONE_ID

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        logger.debug(
            f"🔍 REBAC CHECK: subject={subject_entity}, permission={permission}, object={object_entity}, zone_id={zone_id}"
        )

        # Clean up expired tuples first (this will invalidate affected caches)
        self._cleanup_expired_tuples_if_needed()

        # Check cache first with refresh-ahead (Issue #932)
        # Only if no context, since context makes checks dynamic
        if context is None:
            # Use refresh-ahead pattern to proactively refresh cache before expiry
            if self._l1_cache:
                cached, needs_refresh, cache_key = self._l1_cache.get_with_refresh_check(
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    permission,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    zone_id,
                )
                if cached is not None:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            f"✅ CACHE HIT: result={cached}, needs_refresh={needs_refresh}"
                        )
                    if needs_refresh:
                        # Schedule background refresh without blocking
                        self._schedule_background_refresh(
                            cache_key, subject, permission, object, zone_id
                        )
                    return cached
            else:
                # Fallback to old method if no L1 cache
                cached = self._get_cached_check(subject_entity, permission, object_entity, zone_id)
                if cached is not None:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"✅ CACHE HIT: result={cached}")
                    return cached

            # Cache miss - use stampede prevention (Issue #878)
            # Only one request computes while others wait
            if self._l1_cache:
                should_compute, cache_key = self._l1_cache.try_acquire_compute(
                    subject_entity.entity_type,
                    subject_entity.entity_id,
                    permission,
                    object_entity.entity_type,
                    object_entity.entity_id,
                    zone_id,
                )

                if not should_compute:
                    # Another request is computing - wait for it
                    logger.debug("⏳ STAMPEDE: Waiting for another request to compute")
                    wait_result = self._l1_cache.wait_for_compute(cache_key)
                    if wait_result is not None:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"✅ STAMPEDE: Got result from leader: {wait_result}")
                        return wait_result
                    # Timeout or error - fall through to compute ourselves
                    logger.debug("⚠️ STAMPEDE: Wait timeout, computing ourselves")

                # We're the leader - compute and release
                try:
                    logger.debug("🔎 Computing permission (no cache hit, computing from graph)")
                    start_time = time.perf_counter()
                    result = self._compute_permission(
                        subject_entity,
                        permission,
                        object_entity,
                        visited=set(),
                        depth=0,
                        context=context,
                        zone_id=zone_id,
                    )
                    delta = time.perf_counter() - start_time
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"{'✅' if result else '❌'} REBAC RESULT: {result}")

                    # Cache result and release lock with delta for XFetch (Issue #718)
                    self._l1_cache.release_compute(
                        cache_key,
                        result,
                        subject_entity.entity_type,
                        subject_entity.entity_id,
                        permission,
                        object_entity.entity_type,
                        object_entity.entity_id,
                        zone_id,
                        delta=delta,
                    )
                    # Also cache in L2
                    self._cache_check_result(
                        subject_entity, permission, object_entity, result, zone_id, delta=delta
                    )
                    return result
                except Exception:  # cancel-then-reraise: release L1 compute lock on failure
                    self._l1_cache.cancel_compute(cache_key)
                    raise

        # Context-based check or no L1 cache - compute directly (no stampede prevention)
        logger.debug("🔎 Computing permission (no cache hit, computing from graph)")
        start_time = time.perf_counter()
        result = self._compute_permission(
            subject_entity,
            permission,
            object_entity,
            visited=set(),
            depth=0,
            context=context,
            zone_id=zone_id,
        )
        delta = time.perf_counter() - start_time

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"{'✅' if result else '❌'} REBAC RESULT: {result}")

        # Cache result (only if no context) with delta for XFetch (Issue #718)
        if context is None:
            self._cache_check_result(
                subject_entity, permission, object_entity, result, zone_id, delta=delta
            )

        return result

    # ====================================================================================
    # Batch Check Methods
    # ====================================================================================

    def rebac_check_batch(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        """Batch permission checks for efficiency.

        Checks cache first for each check, then computes uncached checks.
        More efficient than individual checks when checking multiple permissions.

        Args:
            checks: List of (subject, permission, object) tuples to check

        Returns:
            List of boolean results in the same order as input

        Example:
            >>> results = manager.rebac_check_batch([
            ...     (("agent", "alice"), "read", ("file", "f1")),
            ...     (("agent", "alice"), "read", ("file", "f2")),
            ...     (("agent", "bob"), "write", ("file", "f3")),
            ... ])
            >>> # Returns: [True, False, True]
        """
        if not checks:
            return []

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Map to track results by index
        results: dict[int, bool] = {}
        uncached_checks: list[tuple[int, Entity, str, Entity]] = []

        # Phase 1: Check cache for all checks
        for i, (subject, permission, obj) in enumerate(checks):
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])

            cached = self._get_cached_check(subject_entity, permission, object_entity)
            if cached is not None:
                results[i] = cached
            else:
                uncached_checks.append((i, subject_entity, permission, object_entity))

        # Phase 2: Compute uncached checks with delta tracking for XFetch (Issue #718)
        for i, subject_entity, permission, object_entity in uncached_checks:
            start_time = time.perf_counter()
            result = self._compute_permission(
                subject_entity, permission, object_entity, visited=set(), depth=0
            )
            delta = time.perf_counter() - start_time
            self._cache_check_result(
                subject_entity, permission, object_entity, result, zone_id=None, delta=delta
            )
            results[i] = result

        # Return results in original order
        return [results[i] for i in range(len(checks))]

    def rebac_check_batch_fast(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        use_rust: bool = True,
    ) -> list[bool]:
        """Batch permission checks with optional Rust acceleration.

        This method is identical to rebac_check_batch but uses Rust for bulk
        computation of uncached checks, providing 50-85x speedup for large batches.

        Args:
            checks: List of (subject, permission, object) tuples to check
            use_rust: Use Rust acceleration if available (default: True)

        Returns:
            List of boolean results in the same order as input

        Performance:
            - Python only: ~500µs per uncached check
            - Rust acceleration: ~6µs per uncached check (85x speedup)
            - Recommended for batches of 10+ checks

        Example:
            >>> results = manager.rebac_check_batch_fast([
            ...     (("agent", "alice"), "read", ("file", "f1")),
            ...     (("agent", "alice"), "read", ("file", "f2")),
            ...     (("agent", "bob"), "write", ("file", "f3")),
            ... ])
            >>> # Returns: [True, False, True]
        """
        logger = logging.getLogger(__name__)

        if not checks:
            return []

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Map to track results by index
        results: dict[int, bool] = {}
        uncached_checks: list[tuple[int, tuple[tuple[str, str], str, tuple[str, str]]]] = []

        # Phase 1: Check cache for all checks
        for i, (subject, permission, obj) in enumerate(checks):
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])

            cached = self._get_cached_check(subject_entity, permission, object_entity)
            if cached is not None:
                results[i] = cached
            else:
                uncached_checks.append((i, (subject, permission, obj)))

        logger.debug(
            f"🚀 Batch check: {len(checks)} total, {len(results)} cached, "
            f"{len(uncached_checks)} to compute (Rust={'enabled' if use_rust and is_rust_available() else 'disabled'})"
        )

        # Phase 2: Compute uncached checks
        if uncached_checks:
            if use_rust and is_rust_available() and len(uncached_checks) >= 10:
                # Use Rust for bulk computation (efficient for 10+ checks)
                logger.debug(
                    f"⚡ Using Rust acceleration for {len(uncached_checks)} uncached checks"
                )
                try:
                    start_time = time.perf_counter()
                    rust_results = self._compute_batch_rust([check for _, check in uncached_checks])
                    total_delta = time.perf_counter() - start_time
                    # Approximate per-check delta (Rust computes in bulk)
                    avg_delta = total_delta / len(uncached_checks) if uncached_checks else 0.0

                    for idx, (i, _) in enumerate(uncached_checks):
                        result = rust_results[idx]
                        results[i] = result
                        # Cache the result with XFetch delta (Issue #718)
                        subject, permission, obj = uncached_checks[idx][1]
                        subject_entity = Entity(subject[0], subject[1])
                        object_entity = Entity(obj[0], obj[1])
                        self._cache_check_result(
                            subject_entity,
                            permission,
                            object_entity,
                            result,
                            zone_id=None,
                            delta=avg_delta,
                        )
                except Exception as e:  # fail-safe: Rust fallback to Python computation
                    logger.warning(f"Rust batch computation failed, falling back to Python: {e}")
                    # Fall back to Python computation
                    self._compute_batch_python(uncached_checks, results)
            else:
                # Use Python for small batches or when Rust is unavailable
                reason = (
                    "batch too small (<10)" if len(uncached_checks) < 10 else "Rust not available"
                )
                logger.debug(
                    f"🐍 Using Python computation for {len(uncached_checks)} checks ({reason})"
                )
                self._compute_batch_python(uncached_checks, results)

        # Return results in original order
        return [results[i] for i in range(len(checks))]

    def _compute_batch_python(
        self,
        uncached_checks: list[tuple[int, tuple[tuple[str, str], str, tuple[str, str]]]],
        results: dict[int, bool],
    ) -> None:
        """Compute uncached checks using Python (original implementation)."""
        for i, (subject, permission, obj) in uncached_checks:
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])
            start_time = time.perf_counter()
            result = self._compute_permission(
                subject_entity, permission, object_entity, visited=set(), depth=0
            )
            delta = time.perf_counter() - start_time
            self._cache_check_result(
                subject_entity, permission, object_entity, result, zone_id=None, delta=delta
            )
            results[i] = result

    def _compute_batch_rust(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[bool]:
        """Compute multiple permissions using Rust acceleration.

        Args:
            checks: List of (subject, permission, object) tuples

        Returns:
            List of boolean results in same order as input
        """

        # Fetch all relevant tuples from database
        tuples = self._fetch_all_tuples_for_batch(checks)

        # Get all namespace configs needed
        object_types = {obj[0] for _, _, obj in checks}
        namespace_configs: dict[str, Any] = {}
        for obj_type in object_types:
            ns = self.get_namespace(obj_type)
            if ns:
                namespace_configs[obj_type] = {
                    "relations": ns.config.get("relations", {}),
                    "permissions": ns.config.get("permissions", {}),
                }

        # Call Rust extension with tuple version for graph caching
        rust_results_dict = check_permissions_bulk_with_fallback(
            checks, tuples, namespace_configs, force_python=False, tuple_version=self._tuple_version
        )

        # Convert dict results back to list in original order
        results = []
        for subject, permission, obj in checks:
            key = (subject[0], subject[1], permission, obj[0], obj[1])
            results.append(rust_results_dict.get(key, False))

        return results

    def _fetch_all_tuples_for_batch(
        self,
        _checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> list[dict[str, Any]]:
        """Fetch all ReBAC tuples that might be relevant for batch checks.

        This fetches a superset of tuples to minimize database queries.
        """

        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)

            # For simplicity, fetch all tuples (can be optimized later)
            # In production, we'd want to filter by relevant subjects/objects
            cursor.execute(
                self._fix_sql_placeholders(
                    """
                    SELECT subject_type, subject_id, subject_relation,
                           relation, object_type, object_id
                    FROM rebac_tuples
                    WHERE (expiration_time IS NULL OR expiration_time > ?)
                    """
                ),
                (datetime.now(UTC),),
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

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"📦 Fetched {len(tuples)} tuples for batch computation")
            return tuples

    # ====================================================================================
    # Explain Methods
    # ====================================================================================

    def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Explain why a subject has or doesn't have permission on an object.

        This is a debugging/audit API that traces through the permission graph
        to explain the result of a permission check.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., 'read', 'write')
            object: (object_type, object_id) tuple
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            Dictionary with:
            - result: bool - whether permission is granted
            - cached: bool - whether result came from cache
            - reason: str - human-readable explanation
            - paths: list[dict] - all checked paths through the graph
            - successful_path: dict | None - the path that granted access (if any)
            - metadata: dict - request metadata (timestamp, request_id, etc.)

        Example:
            >>> explanation = manager.rebac_explain(
            ...     subject=("agent", "alice_id"),
            ...     permission="read",
            ...     object=("file", "file_id")
            ... )
            >>> print(explanation)
            {
                "result": True,
                "cached": False,
                "reason": "alice has 'viewer' relation via parent inheritance",
                "paths": [
                    {
                        "permission": "read",
                        "expanded_to": ["viewer"],
                        "relation": "viewer",
                        "expanded_to": ["direct_viewer", "parent_viewer", "editor"],
                        "relation": "parent_viewer",
                        "tupleToUserset": {
                            "tupleset": "parent",
                            "found_parents": [("workspace", "ws1")],
                            "computedUserset": "viewer",
                            "found_direct_relation": True
                        }
                    }
                ],
                "successful_path": {...},
                "metadata": {
                    "timestamp": "2025-01-15T10:30:00.123456Z",
                    "request_id": "req_abc123",
                    "max_depth": 10
                }
            }
        """
        # Generate request ID and timestamp
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        timestamp = datetime.now(UTC).isoformat()

        subject_entity = Entity(subject[0], subject[1])
        object_entity = Entity(object[0], object[1])

        # Clean up expired tuples first
        self._cleanup_expired_tuples_if_needed()

        # Check cache first
        cached = self._get_cached_check(subject_entity, permission, object_entity)
        from_cache = cached is not None

        # Track all paths explored
        paths: list[dict[str, Any]] = []

        # Compute permission with path tracking
        result = self._compute_permission_with_explanation(
            subject_entity,
            permission,
            object_entity,
            visited=set(),
            depth=0,
            paths=paths,
            zone_id=zone_id,
        )

        # Find successful path (if any)
        successful_path = None
        for path in paths:
            if path.get("granted"):
                successful_path = path
                break

        # Generate human-readable reason
        if result:
            if from_cache:
                reason = f"{subject_entity} has '{permission}' on {object_entity} (from cache)"
            elif successful_path:
                reason = self._format_path_reason(
                    subject_entity, permission, object_entity, successful_path
                )
            else:
                reason = f"{subject_entity} has '{permission}' on {object_entity}"
        else:
            if from_cache:
                reason = (
                    f"{subject_entity} does NOT have '{permission}' on {object_entity} (from cache)"
                )
            else:
                reason = f"{subject_entity} does NOT have '{permission}' on {object_entity} - no valid path found"

        return {
            "result": result if not from_cache else cached,
            "cached": from_cache,
            "reason": reason,
            "paths": paths,
            "successful_path": successful_path,
            "metadata": {
                "timestamp": timestamp,
                "request_id": request_id,
                "max_depth": self.max_depth,
                "cache_ttl_seconds": self.cache_ttl_seconds,
            },
        }

    def _format_path_reason(
        self, subject: Entity, permission: str, obj: Entity, path: dict[str, Any]
    ) -> str:
        """Format a permission path into a human-readable reason.

        Delegates to PermissionComputer (Issue #1459 Phase 8).
        """
        return PermissionComputer.format_path_reason(subject, permission, obj, path)

    def _compute_permission_with_explanation(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        paths: list[dict[str, Any]],
        zone_id: str | None = None,
    ) -> bool:
        """Compute permission with path tracking. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.compute_permission_with_explanation(
            subject, permission, obj, visited, depth, paths, zone_id
        )

    def _compute_permission(
        self,
        subject: Entity,
        permission: str | dict[str, Any],
        obj: Entity,
        visited: set[tuple[str, str, str, str, str]],
        depth: int,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Compute permission via graph traversal. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.compute_permission(
            subject, permission, obj, visited, depth, context, zone_id
        )

    def _has_direct_relation(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has direct relation. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.has_direct_relation(subject, relation, obj, context, zone_id)

    def _find_direct_relation_tuple(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find direct relation tuple. Delegates to PermissionComputer (Issue #1459)."""
        return self._computer.find_direct_relation_tuple(subject, relation, obj, context, zone_id)

    def _find_subject_sets(
        self, relation: str, obj: Entity, zone_id: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Find all subject sets with a relation to an object. Delegates to TupleRepository."""
        return self._repo.find_subject_sets(relation, obj, zone_id)

    def _find_related_objects(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all objects related to obj via relation. Delegates to TupleRepository."""
        return self._repo.find_related_objects(obj, relation)

    def _find_subjects_with_relation(self, obj: Entity, relation: str) -> list[Entity]:
        """Find all subjects with a relation to obj. Delegates to TupleRepository."""
        return self._repo.find_subjects_with_relation(obj, relation)

    def _evaluate_conditions(
        self, conditions: dict[str, Any] | None, context: dict[str, Any] | None
    ) -> bool:
        """Evaluate ABAC conditions against runtime context. Delegates to TupleRepository."""
        return TupleRepository.evaluate_conditions(conditions, context)

    def _get_direct_subjects(self, relation: str, obj: Entity) -> list[tuple[str, str]]:
        """Get all subjects with direct relation to object. Delegates to TupleRepository."""
        return self._repo.get_direct_subjects(relation, obj)

    # ====================================================================================
    # Cache Methods
    # ====================================================================================

    def _get_cached_check(
        self, subject: Entity, permission: str, obj: Entity, zone_id: str | None = None
    ) -> bool | None:
        """Get cached permission check result.

        Checks L1 (in-memory) cache only. L2 SQL cache (rebac_check_cache) removed.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            zone_id: Optional zone ID

        Returns:
            Cached result or None if not cached or expired
        """
        # Check L1 cache (if enabled)
        if self._l1_cache:
            l1_result = self._l1_cache.get(
                subject.entity_type,
                subject.entity_id,
                permission,
                obj.entity_type,
                obj.entity_id,
                zone_id,
            )
            if l1_result is not None:
                logger.debug("L1 CACHE HIT")
                return l1_result

        # L2 SQL cache removed — return None (cache miss)
        return None

    # ============================================================
    # Background Refresh (Issue #932)
    # ============================================================

    def _schedule_background_refresh(
        self,
        cache_key: str,
        subject: tuple[str, str],
        permission: str,
        obj: tuple[str, str],
        zone_id: str | None,
    ) -> None:
        """Schedule a background refresh for a cache entry.

        This is called when a cache hit occurs but the entry is past its
        refresh threshold. The cached value is returned immediately while
        a background thread refreshes the cache.

        Args:
            cache_key: Cache key being refreshed
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            obj: (object_type, object_id) tuple
            zone_id: Optional zone ID
        """
        if not self._l1_cache:
            return

        if not self._l1_cache.mark_refresh_in_progress(cache_key):
            # Already being refreshed by another thread
            return

        # Start background refresh in a daemon thread
        thread = threading.Thread(
            target=self._background_refresh_worker,
            args=(cache_key, subject, permission, obj, zone_id),
            daemon=True,
            name=f"rebac-refresh-{cache_key[:20]}",
        )
        thread.start()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"🔄 REFRESH: Scheduled background refresh for {cache_key[:50]}...")

    def _background_refresh_worker(
        self,
        cache_key: str,
        subject: tuple[str, str],
        permission: str,
        obj: tuple[str, str],
        zone_id: str | None,
    ) -> None:
        """Worker thread that refreshes a cache entry in the background.

        Args:
            cache_key: Cache key being refreshed
            subject: (subject_type, subject_id) tuple
            permission: Permission to check
            obj: (object_type, object_id) tuple
            zone_id: Optional zone ID
        """
        try:
            subject_entity = Entity(subject[0], subject[1])
            object_entity = Entity(obj[0], obj[1])

            # Compute permission (bypassing cache) and measure delta for XFetch
            start_time = time.perf_counter()
            result = self._compute_permission(
                subject_entity,
                permission,
                object_entity,
                visited=set(),
                depth=0,
                context=None,
                zone_id=zone_id,
            )
            delta = time.perf_counter() - start_time

            # Update cache with delta for XFetch (Issue #718)
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

            # Also update L2 cache
            self._cache_check_result(subject_entity, permission, object_entity, result, zone_id)

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"✅ REFRESH: Background refresh complete for {cache_key[:50]}...")
        except Exception as e:  # fail-safe: background refresh must not crash thread
            logger.warning(f"REFRESH: Background refresh failed for {cache_key[:50]}: {e}")
        finally:
            if self._l1_cache:
                self._l1_cache.complete_refresh(cache_key)

    def _cache_check_result(
        self,
        subject: Entity,
        permission: str,
        obj: Entity,
        result: bool,
        zone_id: str | None = None,
        conn: Any | None = None,
        delta: float = 0.0,
    ) -> None:
        """Cache permission check result in L1 cache.

        L2 SQL cache (rebac_check_cache) has been removed.

        Args:
            subject: Subject entity
            permission: Permission
            obj: Object entity
            result: Check result
            zone_id: Optional zone ID for multi-zone isolation
            conn: Optional database connection (unused, kept for API compatibility)
            delta: Recomputation time in seconds for XFetch (Issue #718)
        """
        # Cache in L1 (in-memory)
        if self._l1_cache:
            self._l1_cache.set(
                subject.entity_type,
                subject.entity_id,
                permission,
                obj.entity_type,
                obj.entity_id,
                result,
                zone_id,
                delta=delta,
            )

        # L2 SQL cache (rebac_check_cache) removed — no-op.

    def _invalidate_cache_for_tuple(
        self,
        subject: Entity,
        relation: str,
        obj: Entity,
        zone_id: str | None = None,
        subject_relation: str | None = None,
        expires_at: datetime | None = None,
        conn: Any | None = None,
    ) -> None:
        """Invalidate caches for tuple change. Delegates to CacheCoordinator."""
        self._cache_coordinator.invalidate_for_tuple_change(
            subject, relation, obj, zone_id, subject_relation, expires_at, conn
        )

    def _invalidate_cache_for_namespace(self, object_type: str) -> None:
        """Invalidate caches for namespace change. Delegates to CacheCoordinator."""
        self._cache_coordinator.invalidate_for_namespace_change(object_type)

    # ====================================================================================
    # Maintenance Methods
    # ====================================================================================

    def _cleanup_expired_tuples_if_needed(self) -> None:
        """Clean up expired tuples if enough time has passed since last cleanup.

        This method throttles cleanup operations to avoid checking on every rebac_check call.
        Only cleans up if more than 1 second has passed since last cleanup.
        """
        now = datetime.now(UTC)

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
        """Remove expired cache entries. Delegates to CacheCoordinator."""
        return self._cache_coordinator.cleanup_expired_cache()

    def cleanup_expired_tuples(self) -> int:
        """Remove expired relationship tuples. Delegates to CacheCoordinator."""
        return self._cache_coordinator.cleanup_expired_tuples()

    # ====================================================================================
    # Stats and Monitoring
    # ====================================================================================

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics. Delegates to CacheCoordinator."""
        return self._cache_coordinator.get_cache_stats()

    def reset_cache_stats(self) -> None:
        """Reset cache statistics. Delegates to CacheCoordinator."""
        self._cache_coordinator.reset_cache_stats()

    def close(self) -> None:
        """Close the manager: shut down cache coordinator and release DB resources.

        Must be called before the underlying database engine is disposed.
        Stops background recompute threads, clears all caches, and releases
        database connection callbacks to prevent use-after-close errors.
        """
        if hasattr(self, "_cache_coordinator") and self._cache_coordinator is not None:
            self._cache_coordinator.close()


# Backward-compat alias — many tests and call-sites still reference the old name.
EnhancedReBACManager = ReBACManager


# ── Async wrapper (merged from async_manager.py, Issue #1385) ────────


class AsyncReBACManager:
    """Async facade over the synchronous ReBACManager.

    Wraps all public methods with ``asyncio.to_thread()`` for non-blocking
    execution in async contexts (FastAPI, etc.).
    """

    def __init__(self, sync_manager: Any) -> None:
        self._sync = sync_manager

    # ── Core Zanzibar APIs ──────────────────────────────────────────

    async def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        context: dict[str, Any] | None = None,
        zone_id: str | None = None,
        consistency: Any | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._sync.rebac_check,
            subject,
            permission,
            object,
            context,
            zone_id,
            consistency,
        )

    async def rebac_write(
        self,
        subject: tuple[str, str] | tuple[str, str, str],
        relation: str,
        object: tuple[str, str],
        expires_at: Any | None = None,
        conditions: dict[str, Any] | None = None,
        zone_id: str | None = None,
    ) -> WriteResult:
        return await asyncio.to_thread(
            self._sync.rebac_write,
            subject,
            relation,
            object,
            expires_at,
            conditions,
            zone_id,
        )

    async def rebac_delete(self, tuple_id: str) -> bool:
        return await asyncio.to_thread(self._sync.rebac_delete, tuple_id)

    async def rebac_expand(
        self,
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self._sync.rebac_expand, permission, object, zone_id)

    # ── Bulk APIs ───────────────────────────────────────────────────

    async def rebac_check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str = ROOT_ZONE_ID,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        return await asyncio.to_thread(self._sync.rebac_check_bulk, checks, zone_id)

    async def rebac_list_objects(
        self,
        subject: tuple[str, str],
        permission: str,
        object_type: str = "file",
        zone_id: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        return await asyncio.to_thread(
            self._sync.rebac_list_objects,
            subject,
            permission,
            object_type,
            zone_id,
            path_prefix,
            limit,
            offset,
        )

    async def rebac_write_batch(self, tuples: list[dict[str, Any]]) -> int:
        return await asyncio.to_thread(self._sync.rebac_write_batch, tuples)

    async def rebac_explain(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._sync.rebac_explain, subject, permission, object, zone_id
        )

    # ── Namespace APIs ──────────────────────────────────────────────

    async def get_namespace(self, object_type: str) -> Any:
        return await asyncio.to_thread(self._sync.get_namespace, object_type)

    async def create_namespace(self, namespace: Any) -> None:
        return await asyncio.to_thread(self._sync.create_namespace, namespace)

    # ── Cache / Leopard / Tiger APIs ────────────────────────────────

    async def get_transitive_groups(
        self,
        subject: tuple[str, str],
        zone_id: str = ROOT_ZONE_ID,
    ) -> set[tuple[str, str]]:
        return await asyncio.to_thread(self._sync.get_transitive_groups, subject, zone_id)

    async def invalidate_zone_graph_cache(self, zone_id: str | None = None) -> None:
        return await asyncio.to_thread(self._sync.invalidate_zone_graph_cache, zone_id)

    async def get_cache_stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._sync.get_cache_stats)

    def get_l1_cache_stats(self) -> dict[str, Any]:
        return self._sync.get_cache_stats()

    # ── Bridge convenience methods ─────────────────────────────────

    async def write_tuple(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        subject_relation: str | None = None,  # noqa: ARG002
    ) -> str:
        result = await self.rebac_write(
            subject=subject, relation=relation, object=object, zone_id=zone_id
        )
        return result.tuple_id

    async def delete_tuple(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        def _delete_by_components() -> bool:
            from nexus.lib.zone import normalize_zone_id

            nz = normalize_zone_id(zone_id)
            with self._sync._connection() as conn:
                cursor = self._sync._create_cursor(conn)
                cursor.execute(
                    self._sync._fix_sql_placeholders(
                        "SELECT tuple_id FROM rebac_tuples "
                        "WHERE subject_type = ? AND subject_id = ? "
                        "AND relation = ? AND object_type = ? AND object_id = ? "
                        "AND zone_id = ?"
                    ),
                    (subject[0], subject[1], relation, object[0], object[1], nz),
                )
                row = cursor.fetchone()
            if not row:
                return False
            return self._sync.rebac_delete(row[0])

        return await asyncio.to_thread(_delete_by_components)

    # ── Lifecycle ───────────────────────────────────────────────────

    async def close(self) -> None:
        return await asyncio.to_thread(self._sync.close)

    # ── Passthrough properties ──────────────────────────────────────

    @property
    def engine(self) -> Any:
        return self._sync.engine

    @property
    def enforce_zone_isolation(self) -> bool:
        return self._sync.enforce_zone_isolation
