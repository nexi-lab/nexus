"""Cache Coordinator - Unified cache invalidation orchestrator.

Consolidates scattered cache invalidation logic from ReBACManager
into a single coordinator that manages all cache layers.

When a permission tuple is written/deleted, the coordinator ensures
all affected caches are properly invalidated in the correct order:
1. Zone graph cache (in-memory tuple cache)
2. L1 permission check cache (targeted by subject + object)
3. Tiger L2 (Dragonfly) cache — explicit delete (Issue #3395)
4. Permission leases — zone-wide clear (Issue #3394)
5. Boundary cache (permission inheritance boundaries)
6. Directory visibility cache (dir listing optimization)
7. Namespace cache — dcache + mount table (Issue #1244)
8. Iterator cache (pagination cursors)
9. DT_STREAM — intra-zone ordered invalidation (Issue #3192)
10. Pub/Sub — cross-zone fire-and-forget hints (Issue #3192)
11. Durable Stream — cross-zone guaranteed delivery (Issue #3396)

Also handles:
- L2 (database) cache invalidation with precise targeting (Issue #2179 Step 2.5)
- Eager cache recomputation for simple direct relations (PR #969)
- Expired tuple/cache cleanup (maintenance)
- Cache statistics (monitoring)
- Read fence watermark advancement (Issue #3396)

Related: Issue #1459, #2179, #1244, #1077, #3396
"""

import concurrent.futures
import logging
import time as time_module
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.bricks.rebac.cache.coordinator_config import (
    CoordinatorConfig,
    DatabaseCallbacks,
    InvalidationChannels,
    RecomputeCallbacks,
)
from nexus.bricks.rebac.cache.invalidation_stream import (
    InvalidationEventType,
    InvalidationStream,
)
from nexus.bricks.rebac.cache.pubsub_invalidation import PubSubInvalidation
from nexus.bricks.rebac.domain import RELATION_TO_PERMISSIONS
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache
    from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream
    from nexus.bricks.rebac.cache.iterator import IteratorCache
    from nexus.bricks.rebac.cache.read_fence import ReadFence
    from nexus.bricks.rebac.cache.result_cache import ReBACPermissionCache
    from nexus.bricks.rebac.domain import Entity

logger = logging.getLogger(__name__)


class CacheCoordinator:
    """Unified cache invalidation orchestrator.

    Replaces scattered invalidation calls in ReBACManager with
    a single entry point for cache coherence.

    Example:
        coordinator = CacheCoordinator(
            l1_cache=l1_cache,
            boundary_cache=boundary_cache,
            iterator_cache=iterator_cache,
        )

        # On write:
        coordinator.invalidate_for_write(
            zone_id=ROOT_ZONE_ID,
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )
    """

    def __init__(
        self,
        l1_cache: "ReBACPermissionCache | None" = None,
        boundary_cache: "PermissionBoundaryCache | None" = None,
        iterator_cache: "IteratorCache | None" = None,
        zone_graph_cache: "MutableMapping[str, Any] | None" = None,
        *,
        # Grouped config (Issue #3396) — preferred
        config: CoordinatorConfig | None = None,
        # Legacy individual params (backward compat — used if config is None)
        connection_factory: "Callable[..., Any] | None" = None,
        get_connection: "Callable[[], Any] | None" = None,
        close_connection: "Callable[[Any], None] | None" = None,
        create_cursor: "Callable[[Any], Any] | None" = None,
        fix_sql: "Callable[[str], str] | None" = None,
        get_namespace_cb: "Callable[[str], Any] | None" = None,
        compute_permission_cb: "Callable[..., bool] | None" = None,
        cache_check_result_cb: "Callable[..., None] | None" = None,
        invalidation_stream: "InvalidationStream | None" = None,
        pubsub: "PubSubInvalidation | None" = None,
        enable_async_recompute: bool = True,
        cache_ttl_seconds: int = 300,
        get_tuple_version: "Callable[[], int] | None" = None,
        set_tuple_version: "Callable[[int], None] | None" = None,
    ) -> None:
        """Initialize the coordinator.

        Accepts either a ``CoordinatorConfig`` (preferred) or individual
        keyword arguments (backward compat).  If ``config`` is provided,
        individual channel/db/recompute params are ignored.
        """
        self._l1_cache = l1_cache
        self._boundary_cache = boundary_cache
        self._iterator_cache = iterator_cache
        self._zone_graph_cache = zone_graph_cache

        # Resolve config — prefer grouped, fall back to individual params
        if config is not None:
            channels = config.channels
            db = config.database
            rc = config.recompute
            _enable_async = config.enable_async_recompute
            _ttl = config.cache_ttl_seconds
            _get_tv = config.get_tuple_version
            _set_tv = config.set_tuple_version
        else:
            channels = InvalidationChannels(stream=invalidation_stream, pubsub=pubsub)
            db = DatabaseCallbacks(
                connection_factory=connection_factory,
                get_connection=get_connection,
                close_connection=close_connection,
                create_cursor=create_cursor,
                fix_sql=fix_sql,
            )
            rc = RecomputeCallbacks(
                get_namespace=get_namespace_cb,
                compute_permission=compute_permission_cb,
                cache_check_result=cache_check_result_cb,
            )
            _enable_async = enable_async_recompute
            _ttl = cache_ttl_seconds
            _get_tv = get_tuple_version
            _set_tv = set_tuple_version

        # Database access
        self._connection_factory = db.connection_factory
        self._get_connection = db.get_connection
        self._close_connection = db.close_connection
        self._create_cursor = db.create_cursor
        self._fix_sql = db.fix_sql

        # Channels (Issue #3192, #3396)
        self._stream = channels.stream
        self._pubsub = channels.pubsub
        self._durable_stream: DurableInvalidationStream | None = channels.durable_stream
        self._read_fence: ReadFence | None = channels.read_fence

        # Eager recompute
        self._get_namespace_cb = rc.get_namespace
        self._compute_permission_cb = rc.compute_permission
        self._cache_check_result_cb = rc.cache_check_result

        # Stats / cleanup
        self._cache_ttl_seconds = _ttl
        self._get_tuple_version = _get_tv
        self._set_tuple_version = _set_tv

        # Callback registries for external caches (boundary, visibility, etc.)
        self._boundary_invalidators: list[
            tuple[str, Callable[[str, str, str, str, str], None]]
        ] = []
        self._visibility_invalidators: list[tuple[str, Callable[[str, str], None]]] = []
        # Namespace cache invalidators: callback(subject_type, subject_id, zone_id)
        # Used by NamespaceManager to invalidate dcache + mount table on grant/revoke (Issue #1244)
        self._namespace_invalidators: list[tuple[str, Callable[[str, str, str], None]]] = []
        # Permission lease invalidators (Issue #3394, #3398):
        # callback(zone_id, subject, relation, object) — path-targeted for direct
        # grants, zone-wide fallback for group/inherited changes (decision 3A/7A).
        self._lease_invalidators: list[
            tuple[str, Callable[[str, tuple[str, str], str, tuple[str, str]], None]]
        ] = []
        # Tiger L2 (Dragonfly) invalidators: callback(subj_type, subj_id, permission, res_type, zone_id)
        # Explicit L2 cache delete on write — replaces TTL-only expiry (Issue #3395)
        self._tiger_l2_invalidators: list[
            tuple[str, Callable[[str, str, str, str, str], None]]
        ] = []

        # Async eager recompute (Issue #3192)
        self._recompute_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._async_recompute_enabled = _enable_async
        self._recompute_submitted = 0
        self._recompute_completed = 0
        self._recompute_failed = 0

        # Metrics
        self._invalidation_count = 0
        self._zone_graph_invalidations = 0
        self._l1_invalidations = 0
        self._tiger_l2_invalidations = 0
        self._lease_invalidations = 0
        self._boundary_invalidations = 0
        self._visibility_invalidations = 0
        self._namespace_invalidations = 0
        self._iterator_invalidations = 0
        self._callback_failure_count = 0

    # ------------------------------------------------------------------
    # Cache setters (for lazy initialization)
    # ------------------------------------------------------------------

    def set_l1_cache(self, cache: "ReBACPermissionCache") -> None:
        """Set the L1 permission check cache."""
        self._l1_cache = cache

    def set_boundary_cache(self, cache: "PermissionBoundaryCache") -> None:
        """Set the boundary cache."""
        self._boundary_cache = cache

    def set_iterator_cache(self, cache: "IteratorCache") -> None:
        """Set the iterator cache."""
        self._iterator_cache = cache

    def set_zone_graph_cache(self, cache: dict[str, Any]) -> None:
        """Set the zone graph cache (shared dict reference)."""
        self._zone_graph_cache = cache

    def set_invalidation_stream(self, stream: "InvalidationStream") -> None:
        """Set the DT_STREAM for ordered intra-zone invalidation.

        When set, existing callback registrations are migrated to stream consumers.
        New callbacks registered after this will also be auto-registered as consumers.
        """
        self._stream = stream
        # Migrate existing callbacks to stream consumers
        self._register_callbacks_as_stream_consumers()

    def set_pubsub(self, pubsub: "PubSubInvalidation") -> None:
        """Set the Pub/Sub for cross-zone invalidation hints."""
        self._pubsub = pubsub

    def set_durable_stream(self, durable: "DurableInvalidationStream") -> None:
        """Set the durable cross-zone invalidation stream (Issue #3396)."""
        self._durable_stream = durable

    def set_read_fence(self, fence: "ReadFence") -> None:
        """Set the read fence for cross-zone staleness detection (Issue #3396)."""
        self._read_fence = fence

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def register_boundary_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, str, str, str, str], None]",
    ) -> None:
        """Register a boundary cache invalidation callback.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(zone_id, subject_type, subject_id, permission, object_path)
        """
        for cid, _ in self._boundary_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._boundary_invalidators.append((callback_id, callback))

    def unregister_boundary_invalidator(self, callback_id: str) -> bool:
        """Unregister a boundary cache invalidation callback."""
        for i, (cid, _) in enumerate(self._boundary_invalidators):
            if cid == callback_id:
                self._boundary_invalidators.pop(i)
                return True
        return False

    def register_visibility_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, str], None]",
    ) -> None:
        """Register a directory visibility cache invalidation callback.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(zone_id, object_path)
        """
        for cid, _ in self._visibility_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._visibility_invalidators.append((callback_id, callback))

    def unregister_visibility_invalidator(self, callback_id: str) -> bool:
        """Unregister a visibility cache invalidation callback."""
        for i, (cid, _) in enumerate(self._visibility_invalidators):
            if cid == callback_id:
                self._visibility_invalidators.pop(i)
                return True
        return False

    def register_namespace_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, str, str], None]",
    ) -> None:
        """Register a namespace cache invalidation callback (Issue #1244).

        Called on every rebac_write/rebac_delete to immediately invalidate the
        affected subject's dcache + mount table entries.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(subject_type, subject_id, zone_id)
        """
        for cid, _ in self._namespace_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._namespace_invalidators.append((callback_id, callback))

    def unregister_namespace_invalidator(self, callback_id: str) -> bool:
        """Unregister a namespace cache invalidation callback."""
        for i, (cid, _) in enumerate(self._namespace_invalidators):
            if cid == callback_id:
                self._namespace_invalidators.pop(i)
                return True
        return False

    def register_lease_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, tuple[str, str], str, tuple[str, str]], None]",
    ) -> None:
        """Register a permission lease invalidation callback (Issue #3394, #3398).

        Called on every rebac_write/rebac_delete.  The callback receives
        the full tuple context so it can decide between path-targeted
        invalidation (direct grants) and zone-wide clear (group/inherited
        changes).  See Issue #3398 decisions 3A/7A.

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(zone_id, subject, relation, object)
        """
        for cid, _ in self._lease_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._lease_invalidators.append((callback_id, callback))

    def unregister_lease_invalidator(self, callback_id: str) -> bool:
        """Unregister a permission lease invalidation callback."""
        for i, (cid, _) in enumerate(self._lease_invalidators):
            if cid == callback_id:
                self._lease_invalidators.pop(i)
                return True
        return False

    def register_tiger_l2_invalidator(
        self,
        callback_id: str,
        callback: "Callable[[str, str, str, str, str], None]",
    ) -> None:
        """Register a Tiger L2 (Dragonfly) cache invalidation callback (Issue #3395).

        Called on every rebac_write/rebac_delete to explicitly invalidate the
        L2 Dragonfly cache instead of relying on TTL-only expiry.

        The coordinator expands relation → permissions internally (same as
        boundary invalidators), so the callback receives individual permissions.

        The callback receives (subject_type, subject_id, permission, resource_type, zone_id).

        Args:
            callback_id: Unique identifier for this callback
            callback: Function(subj_type, subj_id, permission, res_type, zone_id)
        """
        for cid, _ in self._tiger_l2_invalidators:
            if cid == callback_id:
                return  # Already registered
        self._tiger_l2_invalidators.append((callback_id, callback))

    def unregister_tiger_l2_invalidator(self, callback_id: str) -> bool:
        """Unregister a Tiger L2 (Dragonfly) cache invalidation callback."""
        for i, (cid, _) in enumerate(self._tiger_l2_invalidators):
            if cid == callback_id:
                self._tiger_l2_invalidators.pop(i)
                return True
        return False

    # ------------------------------------------------------------------
    # Unified invalidation (L1 + callback-based)
    # ------------------------------------------------------------------

    def invalidate_for_write(
        self,
        zone_id: str,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],  # noqa: A002
        *,
        local_only: bool = False,
    ) -> None:
        """Invalidate all caches after a permission write.

        This is the single entry point that replaces scattered invalidation
        calls across rebac_write(), rebac_write_batch(), and rebac_delete().

        Args:
            zone_id: Zone where the tuple was written
            subject: (subject_type, subject_id)
            relation: Relation that was written
            object: (object_type, object_id)
            local_only: If True, skip cross-zone publishing (steps 9-10).
                Used by the durable stream consumer handler to avoid
                re-broadcasting received invalidations (ping-pong loop).
        """
        self._invalidation_count += 1
        subject_type, subject_id = subject
        object_type, object_id = object

        # 1. Zone graph cache
        self._invalidate_zone_graph(zone_id)

        # 2. L1 permission check cache (targeted)
        self._invalidate_l1(subject_type, subject_id, object_type, object_id, zone_id)

        # 2.5. Tiger L2 (Dragonfly) cache — explicit delete (Issue #3395)
        self._notify_tiger_l2_invalidators(subject_type, subject_id, relation, object_type, zone_id)

        # 3. Permission leases — path-targeted or zone-wide (Issue #3394, #3398)
        self._notify_lease_invalidators(zone_id, subject, relation, object)

        # 4. Boundary cache (external callbacks)
        self._notify_boundary_invalidators(
            zone_id, subject_type, subject_id, relation, object_type, object_id
        )

        # 5. Directory visibility cache (external callbacks)
        self._notify_visibility_invalidators(zone_id, object_type, object_id)

        # 6. Namespace cache — dcache + mount table (Issue #1244)
        self.notify_namespace_invalidators(zone_id, subject_type, subject_id)

        # 7. Iterator cache (zone-level)
        self._invalidate_iterator(zone_id)

        # 8. DT_STREAM: Publish invalidation event for intra-zone consumers (Issue #3192)
        if self._stream:
            self._stream.append(
                InvalidationEventType.L1_CACHE,
                zone_id,
                subject_type=subject_type,
                subject_id=subject_id,
                relation=relation,
                object_type=object_type,
                object_id=object_id,
            )

        # Steps 9-11: Cross-zone publishing — skipped when local_only=True
        # to prevent ping-pong loops when the consumer handler re-enters.
        if not local_only:
            _payload = {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "relation": relation,
                "object_type": object_type,
                "object_id": object_id,
            }

            # 9. Pub/Sub: Publish cross-zone hint (Issue #3192)
            if self._pubsub:
                self._pubsub.publish_invalidation(
                    zone_id=zone_id,
                    layer="all",
                    payload=_payload,
                )
                # Lease-specific hint for cross-zone lease revocation (Issue #3398 decision 4A).
                # Remote zones subscribe to "lease" layer to invalidate their local lease tables.
                self._pubsub.publish_invalidation(
                    zone_id=zone_id,
                    layer="lease",
                    payload=_payload,
                )

            # 10. Durable Stream: Publish cross-zone guaranteed delivery (Issue #3396)
            if self._durable_stream:
                self._durable_stream.publish(
                    target_zone_id=zone_id,
                    payload={
                        "source_zone": zone_id,
                        **_payload,
                    },
                )

    def invalidate_zone_graph(self, zone_id: str | None = None) -> None:
        """Invalidate zone graph cache.

        Public method for direct zone graph invalidation (e.g., cross-zone shares).

        Args:
            zone_id: Specific zone to invalidate, or None to clear all
        """
        self._invalidate_zone_graph(zone_id)

    def close(self) -> None:
        """Shut down the coordinator: stop background work, clear caches, release DB refs.

        Must be called before the underlying database engine is disposed
        to prevent 'Cannot operate on a closed database' errors from
        stale cache refresh or eager recompute queries.
        """
        # Disable eager recompute — prevents new background DB queries
        self._async_recompute_enabled = False

        # Shut down the recompute executor — wait for in-flight tasks to finish
        # before nulling connection callbacks, preventing 'Cannot operate on a
        # closed database' errors from background threads (macOS CI flake).
        if self._recompute_executor is not None:
            self._recompute_executor.shutdown(wait=True, cancel_futures=True)
            self._recompute_executor = None

        # Release database connection callbacks — prevents any future DB access
        self._connection_factory = None
        self._get_connection = None
        self._close_connection = None
        self._create_cursor = None
        self._compute_permission_cb = None
        self._cache_check_result_cb = None
        self._get_namespace_cb = None

        # Note: don't set _stream = None here — the InvalidationStream may hold
        # references to Rust objects whose Drop can segfault during teardown.

    def invalidate_all(self, zone_id: str | None = None) -> None:
        """Nuclear option: invalidate all caches for a zone (or all zones).

        Use sparingly - prefer targeted invalidation via invalidate_for_write().

        Args:
            zone_id: Zone to invalidate, or None for all zones
        """
        self._invalidate_zone_graph(zone_id)

        if self._l1_cache:
            self._l1_cache.clear()

        # Permission leases — zone-wide clear (nuclear option)
        self._notify_lease_invalidators(
            zone_id or ROOT_ZONE_ID,
            ("*", "*"),  # wildcard subject → forces zone-wide clear
            "*",
            ("*", "*"),
        )

        if self._boundary_cache:
            self._boundary_cache.clear()

        if self._iterator_cache:
            if zone_id:
                self._iterator_cache.invalidate_zone(zone_id)
            else:
                self._iterator_cache.clear()

    # ------------------------------------------------------------------
    # Deep invalidation (L1 + L2 database) — Issue #2179 Step 2.5
    # ------------------------------------------------------------------

    def invalidate_for_tuple_change(
        self,
        subject: "Entity",
        relation: str,
        obj: "Entity",
        zone_id: str | None = None,
        subject_relation: str | None = None,
        expires_at: datetime | None = None,
        conn: Any | None = None,
    ) -> None:
        """Invalidate and optionally recompute cache entries affected by tuple change.

        When a tuple is added or removed, we need to invalidate cache entries that
        might be affected. This uses PRECISE invalidation to minimize cache churn:

        1. Direct: Invalidate (subject, *, object) - permissions on this specific pair
        2. Transitive (if subject has subject_relation): Invalidate members of this group
        3. Transitive (for object): Invalidate derived permissions on related objects

        OPTIMIZATION: For simple direct relations, we RECOMPUTE and UPDATE the cache
        instead of just invalidating. This means the next read is instant (<1ms) instead
        of requiring expensive graph traversal (50-500ms).

        Args:
            subject: Subject entity
            relation: Relation type (used for precise invalidation)
            obj: Object entity
            zone_id: Optional zone ID for zone-scoped invalidation
            subject_relation: Optional subject relation for userset-as-subject
            expires_at: Optional expiration time (disables eager recomputation)
            conn: Optional database connection to reuse
        """
        effective_zone_id = zone_id if zone_id is not None else ROOT_ZONE_ID

        # Zone graph cache must be invalidated so fresh computes see the new tuples
        self._invalidate_zone_graph(effective_zone_id)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "invalidate_for_tuple_change: %s %s -> %s, zone=%s",
                subject,
                relation,
                obj,
                effective_zone_id,
            )

        # Track write for adaptive TTL (Phase 4)
        if self._l1_cache:
            self._l1_cache.track_write(obj.entity_id)

        # Use provided connection or create new one (avoids SQLite lock contention)
        should_close = conn is None
        if conn is None and self._get_connection is not None:
            conn = self._get_connection()
        if conn is None:
            return  # No DB access configured

        try:
            # 1. DIRECT: For simple direct relations, try to eagerly recompute
            should_eager_recompute = (
                expires_at is None
                and subject_relation is None
                and relation not in ("member-of", "member", "parent")
                and subject.entity_type != "*"
                and subject.entity_id != "*"
            )

            # BUG FIX (PR #969): ALWAYS invalidate L1 cache first
            if self._l1_cache:
                self._l1_cache.invalidate_subject_object_pair(
                    subject.entity_type,
                    subject.entity_id,
                    obj.entity_type,
                    obj.entity_id,
                    zone_id,
                )

            # L2 SQL cache (rebac_check_cache) removed — table no longer exists.
            # Invalidation is now handled entirely by L1 in-memory cache above.

            if should_eager_recompute:
                if self._async_recompute_enabled:
                    self._submit_eager_recompute(subject, relation, obj, zone_id)
                else:
                    self._eager_recompute(subject, relation, obj, zone_id, conn)

            # 2. TRANSITIVE (Groups): If subject is a group/set, invalidate cache
            #    for potential members accessing the object.
            #    Use the already-known subject_relation parameter instead of
            #    querying the DB (avoids SQLite lock contention under xdist).
            if subject_relation is not None and self._l1_cache:
                self._l1_cache.invalidate_object(obj.entity_type, obj.entity_id, zone_id)

            # 3. TRANSITIVE (Hierarchy): membership changes invalidate subject's permissions
            if relation in ("member-of", "member", "parent") and self._l1_cache:
                self._l1_cache.invalidate_subject(subject.entity_type, subject.entity_id, zone_id)

            # 4. PARENT PERMISSION CHANGE: invalidate child paths
            if (
                obj.entity_type == "file"
                and relation
                in (
                    "direct_owner",
                    "direct_editor",
                    "direct_viewer",
                    "owner",
                    "editor",
                    "viewer",
                    "shared-viewer",
                    "shared-editor",
                    "shared-owner",
                )
                and self._l1_cache
            ):
                self._l1_cache.invalidate_object_prefix(obj.entity_type, obj.entity_id, zone_id)

            # 5. USERSET-AS-SUBJECT: clear ALL cache (aggressive but safe)
            if subject_relation is not None:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Userset-as-subject detected (%s#%s), clearing ALL cache for safety",
                        subject,
                        subject_relation,
                    )
                if self._l1_cache:
                    self._l1_cache.clear()

            # 6. BOUNDARY CACHE: invalidate cached path-inheritance boundaries
            self._notify_boundary_invalidators(
                effective_zone_id,
                subject.entity_type,
                subject.entity_id,
                relation,
                obj.entity_type,
                obj.entity_id,
            )

            conn.commit()
        finally:
            if should_close and self._close_connection is not None:
                self._close_connection(conn)

    def _submit_eager_recompute(
        self,
        subject: "Entity",
        relation: str,
        obj: "Entity",
        zone_id: str | None,
    ) -> None:
        """Submit eager recomputation as async task.

        Instead of blocking the invalidation path, schedule recomputation
        in a background thread. Cache will be filled when complete.

        If the cache is read before recomputation finishes, the normal
        compute path handles it (protected by stampede prevention).
        """
        if self._recompute_executor is None:
            self._recompute_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="eager-recompute"
            )

        def _do_recompute() -> None:
            try:
                # Need a fresh connection for the background thread
                conn = None
                if self._get_connection is not None:
                    conn = self._get_connection()
                if conn is None:
                    return
                try:
                    self._eager_recompute(subject, relation, obj, zone_id, conn)
                    self._recompute_completed += 1
                finally:
                    if self._close_connection is not None:
                        self._close_connection(conn)
            except Exception as e:
                self._recompute_failed += 1
                logger.warning("Async eager recompute failed: %s", e)

        try:
            self._recompute_executor.submit(_do_recompute)
            self._recompute_submitted += 1
        except RuntimeError:
            # Executor shut down
            pass

    def _eager_recompute(
        self,
        subject: "Entity",
        relation: str,
        obj: "Entity",
        zone_id: str | None,
        conn: Any,
    ) -> None:
        """Eagerly recompute and cache permissions for simple direct relations.

        Instead of just invalidating, we recompute the permission so the next
        read is instant (<1ms) instead of requiring graph traversal (50-500ms).
        """
        if not (
            self._get_namespace_cb and self._compute_permission_cb and self._cache_check_result_cb
        ):
            return

        namespace = self._get_namespace_cb(obj.entity_type)
        if not (namespace and namespace.config and "relations" in namespace.config):
            return

        # Find permissions that this relation affects
        affected_permissions: list[str] = []
        relations = namespace.config.get("relations", {})
        for perm, rel_spec in relations.items():
            if isinstance(rel_spec, dict) and "union" in rel_spec and relation in rel_spec["union"]:
                affected_permissions.append(perm)

        for permission in affected_permissions[:5]:  # Limit to 5 most common
            try:
                start_time = time_module.perf_counter()
                result = self._compute_permission_cb(
                    subject,
                    permission,
                    obj,
                    set(),  # visited
                    0,  # depth
                    None,  # context
                    zone_id,
                )
                delta = time_module.perf_counter() - start_time
                self._cache_check_result_cb(subject, permission, obj, result, zone_id, conn, delta)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Eager cache update: (%s, %s, %s) = %s",
                        subject,
                        permission,
                        obj,
                        result,
                    )
            except Exception as e:
                logger.warning(
                    "Eager recomputation failed for permission %s, continuing: %s", permission, e
                )
                continue

    def invalidate_for_namespace_change(self, object_type: str) -> None:
        """Invalidate all cache entries for objects of a given type in both L1 and L2.

        When a namespace configuration is updated, all cached permission checks
        for objects of that type may be stale and must be invalidated.

        Args:
            object_type: Type of object whose namespace was updated
        """
        # L1 cache invalidation - clear all (conservative approach)
        if self._l1_cache:
            self._l1_cache.clear()
            logger.info("Cleared L1 cache due to namespace '%s' config update", object_type)

        # L2 SQL cache (rebac_check_cache) removed — no-op.

    # ------------------------------------------------------------------
    # Maintenance — Issue #2179 Step 2.5
    # ------------------------------------------------------------------

    def cleanup_expired_cache(self) -> int:
        """Remove expired cache entries.

        Returns:
            Number of cache entries removed

        Note: L2 SQL cache (rebac_check_cache) removed — always returns 0.
        """
        return 0

    def cleanup_expired_tuples(self) -> int:
        """Remove expired relationship tuples and invalidate their caches.

        Returns:
            Number of tuples removed
        """
        if not (self._connection_factory and self._fix_sql and self._create_cursor):
            return 0

        from nexus.bricks.rebac.domain import Entity

        with self._connection_factory() as conn:
            cursor = self._create_cursor(conn)

            # Get expired tuples for changelog
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT tuple_id, subject_type, subject_id, subject_relation,
                           relation, object_type, object_id, zone_id
                    FROM rebac_tuples
                    WHERE expires_at IS NOT NULL AND expires_at <= ?
                    """
                ),
                (datetime.now(UTC).isoformat(),),
            )
            expired_tuples = cursor.fetchall()

            # Delete expired tuples
            cursor.execute(
                self._fix_sql(
                    """
                    DELETE FROM rebac_tuples
                    WHERE expires_at IS NOT NULL AND expires_at <= ?
                    """
                ),
                (datetime.now(UTC).isoformat(),),
            )

            # Log to changelog and invalidate caches for expired tuples
            for row in expired_tuples:
                tuple_id = row["tuple_id"]
                subject_type = row["subject_type"]
                subject_id = row["subject_id"]
                subject_relation = row["subject_relation"]
                relation = row["relation"]
                object_type = row["object_type"]
                object_id = row["object_id"]
                zone_id = row["zone_id"]

                # Issue #773: include zone_id in changelog
                cursor.execute(
                    self._fix_sql(
                        """
                        INSERT INTO rebac_changelog (
                            change_type, tuple_id, subject_type, subject_id,
                            relation, object_type, object_id, zone_id, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        "DELETE",
                        tuple_id,
                        subject_type,
                        subject_id,
                        relation,
                        object_type,
                        object_id,
                        zone_id or ROOT_ZONE_ID,
                        datetime.now(UTC).isoformat(),
                    ),
                )

                # Invalidate cache for this tuple
                # Pass a dummy expires_at to prevent eager recomputation during cleanup
                # FIX: Pass conn to avoid opening new connection (pool exhaustion)
                subject = Entity(subject_type, subject_id)
                obj = Entity(object_type, object_id)
                self.invalidate_for_tuple_change(
                    subject,
                    relation,
                    obj,
                    zone_id,
                    subject_relation,
                    expires_at=datetime.now(UTC),
                    conn=conn,
                )

            conn.commit()
            if expired_tuples and self._set_tuple_version and self._get_tuple_version:
                self._set_tuple_version(self._get_tuple_version() + 1)
            return len(expired_tuples)

    # ------------------------------------------------------------------
    # Stats and monitoring — Issue #2179 Step 2.5
    # ------------------------------------------------------------------

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics for monitoring and debugging.

        Returns statistics about L1 (in-memory) cache performance.
        L2 SQL cache (rebac_check_cache) has been removed.
        """
        stats: dict[str, Any] = {
            "l1_enabled": self._l1_cache is not None,
            "l2_enabled": False,
            "l2_ttl_seconds": self._cache_ttl_seconds,
        }

        # L1 cache stats
        if self._l1_cache:
            stats["l1_stats"] = self._l1_cache.get_stats()
        else:
            stats["l1_stats"] = None

        # L2 SQL cache removed — always 0
        stats["l2_size"] = 0

        return stats

    def reset_cache_stats(self) -> None:
        """Reset cache statistics counters.

        Useful for benchmarking and monitoring. Resets hit/miss counters
        and timing metrics for L1 cache.

        Note: Only resets metrics, does not clear cache entries.
        """
        if self._l1_cache:
            self._l1_cache.reset_stats()
            logger.info("Cache statistics reset")

    # ------------------------------------------------------------------
    # Generic dispatch helper (Issue #3396 decision 5A — DRY)
    # ------------------------------------------------------------------

    def _dispatch_to_layer(
        self,
        event_type: InvalidationEventType,
        zone_id: str,
        callbacks: "list[tuple[str, Callable[..., None]]]",
        callback_invoker: "Callable[[Callable[..., None]], None]",
        *,
        stream_payload: dict[str, Any] | None = None,
        metric_attr: str | None = None,
    ) -> None:
        """Dispatch an invalidation event to stream consumers or callbacks.

        Consolidates the repeated pattern: if DT_STREAM → append to stream,
        else → loop callbacks with try/except isolation.

        Args:
            event_type: InvalidationEventType for stream dispatch.
            zone_id: Zone where the invalidation occurred.
            callbacks: List of (callback_id, callback_fn) pairs.
            callback_invoker: Closure that calls a single callback with the
                right arguments (varies per layer).
            stream_payload: Payload dict for stream.append() (if stream active).
            metric_attr: Optional attribute name to increment (e.g. '_boundary_invalidations').
        """
        if not callbacks and not self._stream:
            return

        if metric_attr:
            setattr(self, metric_attr, getattr(self, metric_attr, 0) + 1)

        if self._stream and stream_payload is not None:
            self._stream.append(event_type, zone_id, **stream_payload)
        else:
            for callback_id, callback in callbacks:
                try:
                    callback_invoker(callback)
                except Exception:
                    self._callback_failure_count += 1
                    logger.warning(
                        "[CacheCoordinator] Callback %s failed for %s",
                        callback_id,
                        event_type.value,
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # Internal helpers (L1 + zone graph)
    # ------------------------------------------------------------------

    def _invalidate_zone_graph(self, zone_id: str | None = None) -> None:
        """Invalidate zone graph cache entries."""
        if self._zone_graph_cache is None:
            return

        self._zone_graph_invalidations += 1

        if zone_id is None:
            self._zone_graph_cache.clear()
        elif zone_id in self._zone_graph_cache:
            del self._zone_graph_cache[zone_id]

    def _register_callbacks_as_stream_consumers(self) -> None:
        """Migrate existing callback registrations to DT_STREAM consumers.

        Each callback becomes a stream consumer that filters by event type
        and dispatches to the original callback function.
        """
        if not self._stream:
            return

        for callback_id, callback in self._boundary_invalidators:

            def _make_boundary_handler(cb: Any) -> Any:
                def handler(event: Any) -> None:
                    if event.event_type == InvalidationEventType.BOUNDARY:
                        for perm in event.payload.get(
                            "permissions", [event.payload.get("relation", "")]
                        ):
                            cb(
                                event.zone_id,
                                event.payload["subject_type"],
                                event.payload["subject_id"],
                                perm,
                                event.payload["object_id"],
                            )

                return handler

            self._stream.register_consumer(
                f"boundary:{callback_id}",
                _make_boundary_handler(callback),
                [InvalidationEventType.BOUNDARY],
            )

        for vis_id, vis_cb in self._visibility_invalidators:

            def _make_visibility_handler(cb: Any) -> Any:
                def handler(event: Any) -> None:
                    if event.event_type == InvalidationEventType.VISIBILITY:
                        cb(event.zone_id, event.payload["object_id"])

                return handler

            self._stream.register_consumer(
                f"visibility:{vis_id}",
                _make_visibility_handler(vis_cb),
                [InvalidationEventType.VISIBILITY],
            )

        for ns_id, ns_cb in self._namespace_invalidators:

            def _make_namespace_handler(cb: Any) -> Any:
                def handler(event: Any) -> None:
                    if event.event_type == InvalidationEventType.NAMESPACE:
                        cb(
                            event.payload["subject_type"],
                            event.payload["subject_id"],
                            event.zone_id,
                        )

                return handler

            self._stream.register_consumer(
                f"namespace:{ns_id}",
                _make_namespace_handler(ns_cb),
                [InvalidationEventType.NAMESPACE],
            )

    def _invalidate_l1(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> None:
        """Invalidate L1 permission cache for subject and object."""
        if self._l1_cache is None:
            return

        self._l1_invalidations += 1
        self._l1_cache.invalidate_subject(subject_type, subject_id, zone_id)
        self._l1_cache.invalidate_object(object_type, object_id, zone_id)

    def _notify_lease_invalidators(
        self,
        zone_id: str,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],  # noqa: A002
    ) -> None:
        """Notify permission lease invalidators (Issue #3394, #3398).

        Passes the full tuple context so callbacks can decide between
        path-targeted invalidation (direct grants on files) and zone-wide
        clear (group/inherited changes).  See decisions 3A/7A.

        Called after L1 cache invalidation and before boundary cache
        invalidation to ensure stale leases cannot be used during the
        boundary recomputation window.
        """
        if not self._lease_invalidators:
            return

        self._lease_invalidations += 1

        for callback_id, callback in self._lease_invalidators:
            try:
                callback(zone_id, subject, relation, object)
            except Exception:
                self._callback_failure_count += 1
                logger.warning(
                    "[CacheCoordinator] Lease invalidator %s failed for zone %s",
                    callback_id,
                    zone_id,
                    exc_info=True,
                )

    def _notify_tiger_l2_invalidators(
        self,
        subject_type: str,
        subject_id: str,
        relation: str,
        resource_type: str,
        zone_id: str,
    ) -> None:
        """Notify Tiger L2 (Dragonfly) cache invalidators (Issue #3395).

        Called after L1 cache invalidation and before permission lease
        invalidation.  Each callback performs an explicit Dragonfly DEL
        instead of relying on TTL-only expiry.

        Expands relation → permissions (same mapping as boundary invalidators)
        because Tiger cache keys are keyed by permission, not relation.
        """
        if not self._tiger_l2_invalidators:
            return

        # Map relation to permissions — same as boundary invalidators
        permissions = RELATION_TO_PERMISSIONS.get(relation, [relation])

        self._tiger_l2_invalidations += 1

        for callback_id, callback in self._tiger_l2_invalidators:
            for permission in permissions:
                try:
                    callback(subject_type, subject_id, permission, resource_type, zone_id)
                except Exception:
                    self._callback_failure_count += 1
                    logger.warning(
                        "[CacheCoordinator] Tiger L2 invalidator %s failed for %s",
                        callback_id,
                        permission,
                        exc_info=True,
                    )

    def _notify_boundary_invalidators(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        relation: str,
        object_type: str,
        object_id: str,
    ) -> None:
        """Notify boundary cache invalidators."""
        if not self._boundary_invalidators:
            return

        # Only invalidate for file objects
        if object_type not in ("file", "memory", "resource"):
            return

        # Map relation to permissions
        permissions = RELATION_TO_PERMISSIONS.get(relation, [relation])

        def _invoke_boundary(cb: "Callable[..., None]") -> None:
            for permission in permissions:
                cb(zone_id, subject_type, subject_id, permission, object_id)

        self._dispatch_to_layer(
            InvalidationEventType.BOUNDARY,
            zone_id,
            self._boundary_invalidators,
            _invoke_boundary,
            stream_payload={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "relation": relation,
                "object_type": object_type,
                "object_id": object_id,
                "permissions": permissions,
            },
            metric_attr="_boundary_invalidations",
        )

        # Always invalidate the internal boundary cache directly
        if self._boundary_cache:
            for permission in permissions:
                self._boundary_cache.invalidate_permission_change(
                    zone_id, subject_type, subject_id, permission, object_id
                )

    def _notify_visibility_invalidators(
        self,
        zone_id: str,
        object_type: str,
        object_id: str,
    ) -> None:
        """Notify directory visibility cache invalidators."""
        if not self._visibility_invalidators and not self._stream:
            return

        # Only invalidate for file objects
        if object_type not in ("file", "memory", "resource"):
            return

        self._dispatch_to_layer(
            InvalidationEventType.VISIBILITY,
            zone_id,
            self._visibility_invalidators,
            lambda cb: cb(zone_id, object_id),
            stream_payload={"object_type": object_type, "object_id": object_id},
            metric_attr="_visibility_invalidations",
        )

    def notify_namespace_invalidators(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
    ) -> None:
        """Notify namespace cache invalidators (Issue #1244).

        Public entry point for namespace cache invalidation. When a permission
        tuple changes for a subject, invalidate that subject's dcache + mount
        table so visibility reflects the new grants immediately.

        Args:
            zone_id: Zone where the tuple was written
            subject_type: Type of the subject whose cache should be invalidated
            subject_id: ID of the subject whose cache should be invalidated
        """
        if not self._namespace_invalidators and not self._stream:
            return

        self._dispatch_to_layer(
            InvalidationEventType.NAMESPACE,
            zone_id,
            self._namespace_invalidators,
            lambda cb: cb(subject_type, subject_id, zone_id),
            stream_payload={"subject_type": subject_type, "subject_id": subject_id},
            metric_attr="_namespace_invalidations",
        )

    def _invalidate_iterator(self, zone_id: str) -> None:
        """Invalidate iterator cache for a zone."""
        if self._iterator_cache is None:
            return

        self._iterator_invalidations += 1
        self._iterator_cache.invalidate_zone(zone_id)

    # ------------------------------------------------------------------
    # Coordinator metrics
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Get coordinator statistics."""
        stats: dict[str, Any] = {
            "total_invalidations": self._invalidation_count,
            "zone_graph_invalidations": self._zone_graph_invalidations,
            "l1_invalidations": self._l1_invalidations,
            "tiger_l2_invalidations": self._tiger_l2_invalidations,
            "lease_invalidations": self._lease_invalidations,
            "boundary_invalidations": self._boundary_invalidations,
            "visibility_invalidations": self._visibility_invalidations,
            "namespace_invalidations": self._namespace_invalidations,
            "iterator_invalidations": self._iterator_invalidations,
            "registered_boundary_invalidators": len(self._boundary_invalidators),
            "registered_visibility_invalidators": len(self._visibility_invalidators),
            "registered_namespace_invalidators": len(self._namespace_invalidators),
            "registered_lease_invalidators": len(self._lease_invalidators),
            "registered_tiger_l2_invalidators": len(self._tiger_l2_invalidators),
            "callback_failure_count": self._callback_failure_count,
            "async_recompute_submitted": self._recompute_submitted,
            "async_recompute_completed": self._recompute_completed,
            "async_recompute_failed": self._recompute_failed,
            "stream_enabled": self._stream is not None,
            "pubsub_enabled": self._pubsub is not None,
            "durable_stream_enabled": self._durable_stream is not None,
            "read_fence_enabled": self._read_fence is not None,
        }
        if self._durable_stream:
            stats["durable_stream"] = self._durable_stream.stats()
        if self._read_fence:
            stats["read_fence"] = self._read_fence.stats()
        return stats

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        self._invalidation_count = 0
        self._zone_graph_invalidations = 0
        self._l1_invalidations = 0
        self._tiger_l2_invalidations = 0
        self._lease_invalidations = 0
        self._boundary_invalidations = 0
        self._visibility_invalidations = 0
        self._namespace_invalidations = 0
        self._iterator_invalidations = 0
        self._callback_failure_count = 0
        self._recompute_submitted = 0
        self._recompute_completed = 0
        self._recompute_failed = 0
