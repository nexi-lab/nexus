"""Zone Graph Loader — fetches and caches zone tuples for permission checks.

Extracts zone-tuple fetching, cross-zone share loading, wildcard loading,
and the in-memory LRU zone graph cache from ReBACManager.

Related: Issue #2179 (decomposition), Issue #1459 (LRU capping)
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cachetools import LRUCache

from nexus.contracts.rebac_types import CROSS_ZONE_ALLOWED_RELATIONS

if TYPE_CHECKING:
    from nexus.bricks.rebac.cache.leopard_facade import LeopardFacade
    from nexus.bricks.rebac.cache.shared_ring_buffer import SharedRingBuffer
    from nexus.bricks.rebac.domain import Entity

logger = logging.getLogger(__name__)


class ZoneGraphLoader:
    """Loads and caches zone tuples for graph-based permission checks.

    Owns the ``_zone_graph_cache`` LRUCache and all fetch helpers that were
    formerly inlined in ``ReBACManager``:
    - ``_fetch_tuples_for_rust``
    - ``_get_cached_zone_tuples`` / ``_cache_zone_tuples``
    - ``get_zone_tuples``
    - ``_fetch_zone_tuples_from_db``
    - ``_fetch_cross_zone_shares``
    - ``_fetch_cross_zone_wildcards``
    - ``invalidate_zone_graph_cache``
    """

    def __init__(
        self,
        connection_factory: Callable[..., Any],
        create_cursor: Callable[[Any], Any],
        fix_sql: Callable[[str], str],
        get_namespace_configs_for_rust: Callable[[], dict[str, Any]],
        leopard_facade: "LeopardFacade",
        cache_ttl: int = 300,
        max_zones: int = 100,
    ) -> None:
        self._connection = connection_factory
        self._create_cursor = create_cursor
        self._fix_sql = fix_sql
        self._get_namespace_configs_for_rust = get_namespace_configs_for_rust
        self._leopard_facade = leopard_facade

        self._cache: LRUCache[str, tuple[list[dict[str, Any]], dict[str, Any], float]] = LRUCache(
            maxsize=max_zones
        )
        self._cache_ttl = cache_ttl
        self._cache_lock = threading.RLock()

        # SharedRingBuffer for cross-process zone tuple broadcasting (Issue #3192)
        # When set, zone tuple updates are published to the ring buffer so other
        # processes can read from mmap instead of querying the database.
        self._ring_buffer: "SharedRingBuffer | None" = None

    def set_ring_buffer(self, ring_buffer: "SharedRingBuffer") -> None:
        """Set the SharedRingBuffer for cross-process tuple broadcasting."""
        self._ring_buffer = ring_buffer

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_tuples_for_rust(
        self,
        zone_id: str,
        subject: "Entity | None" = None,
    ) -> list[dict[str, Any]]:
        """Fetch ReBAC tuples for Rust permission computation with caching.

        Cache strategy:
        - Zone tuples: Cached with TTL (the O(T) part)
        - Cross-zone shares: Always fresh (small, indexed query)
        """
        cached_tuples = self._get_cached(zone_id)

        if cached_tuples is not None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[GRAPH-CACHE] Cache HIT for zone %s: %d tuples", zone_id, len(cached_tuples)
                )
            tuples = list(cached_tuples)
        else:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[GRAPH-CACHE] Cache MISS for zone %s, fetching from DB", zone_id)
            tuples = self._fetch_from_db(zone_id)
            self._put(zone_id, tuples)

            # Publish to ring buffer for cross-process sharing (Issue #3192)
            if self._ring_buffer:
                try:
                    import json

                    self._ring_buffer.write(
                        json.dumps({"zone_id": zone_id, "count": len(tuples)}).encode()
                    )
                except Exception:
                    logger.debug("[GRAPH-CACHE] Ring buffer write failed for zone %s", zone_id)

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[GRAPH-CACHE] Cached %d tuples for zone %s", len(tuples), zone_id)

        # Cross-zone shares: always fresh
        if subject is not None:
            cross_zone_tuples = self._fetch_cross_zone_shares(zone_id, subject)
            if cross_zone_tuples:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "[GRAPH-CACHE] Fetched %d cross-zone shares for %s",
                        len(cross_zone_tuples),
                        subject,
                    )
                tuples.extend(cross_zone_tuples)

        # Wildcard tuples (*:*) from other zones
        wildcard_tuples = self._fetch_cross_zone_wildcards(zone_id)
        if wildcard_tuples:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[GRAPH-CACHE] Fetched %d cross-zone wildcard tuples", len(wildcard_tuples)
                )
            tuples.extend(wildcard_tuples)

        # Leopard synthetic membership tuples
        if subject is not None:
            self._leopard_facade.add_synthetic_tuples(
                tuples,
                subject.entity_type,
                subject.entity_id,
                zone_id,
            )

        return tuples

    def get_zone_tuples(self, zone_id: str) -> list[dict[str, Any]]:
        """Fetch all permission tuples for a zone (for export/portability)."""
        return self._fetch_from_db(zone_id)

    def invalidate(self, zone_id: str | None = None) -> None:
        """Invalidate the zone graph cache.

        Args:
            zone_id: Specific zone to invalidate, or None to clear all.
        """
        with self._cache_lock:
            if zone_id is None:
                count = len(self._cache)
                self._cache.clear()
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[GRAPH-CACHE] Cleared all %d cached zone graphs", count)
            elif zone_id in self._cache:
                del self._cache[zone_id]
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[GRAPH-CACHE] Invalidated cache for zone %s", zone_id)

    @property
    def raw_cache(self) -> LRUCache:
        """Expose underlying LRU cache (for CacheCoordinator wiring)."""
        return self._cache

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_cached(self, zone_id: str) -> list[dict[str, Any]] | None:
        with self._cache_lock:
            if zone_id not in self._cache:
                return None
            tuples, _ns, cached_at = self._cache[zone_id]
            if time.perf_counter() - cached_at > self._cache_ttl:
                del self._cache[zone_id]
                return None
            return list(tuples)

    def _put(self, zone_id: str, tuples: list[dict[str, Any]]) -> None:
        ns_configs = self._get_namespace_configs_for_rust()
        with self._cache_lock:
            self._cache[zone_id] = (tuples, ns_configs, time.perf_counter())

    def _fetch_from_db(self, zone_id: str) -> list[dict[str, Any]]:
        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id, conditions
                    FROM rebac_tuples
                    WHERE zone_id = ?
                      AND (expires_at IS NULL OR expires_at > ?)
                    LIMIT 10000
                    """
                ),
                (zone_id, datetime.now(UTC).isoformat()),
            )
            return [
                {
                    "subject_type": row["subject_type"],
                    "subject_id": row["subject_id"],
                    "subject_relation": row["subject_relation"],
                    "relation": row["relation"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "conditions": row["conditions"],
                }
                for row in cursor.fetchall()
            ]

    def _fetch_cross_zone_shares(
        self,
        zone_id: str,
        subject: "Entity",
    ) -> list[dict[str, Any]]:
        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)
            cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
            placeholders = ", ".join("?" * len(cross_zone_relations))

            cursor.execute(
                self._fix_sql(
                    f"""
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id, conditions
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
            return [
                {
                    "subject_type": row["subject_type"],
                    "subject_id": row["subject_id"],
                    "subject_relation": row["subject_relation"],
                    "relation": row["relation"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "conditions": row["conditions"],
                }
                for row in cursor.fetchall()
            ]

    def _fetch_cross_zone_wildcards(self, zone_id: str) -> list[dict[str, Any]]:
        from nexus.bricks.rebac.domain import WILDCARD_SUBJECT

        with self._connection(readonly=True) as conn:
            cursor = self._create_cursor(conn)
            cursor.execute(
                self._fix_sql(
                    """
                    SELECT subject_type, subject_id, subject_relation, relation,
                           object_type, object_id, conditions
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
            return [
                {
                    "subject_type": row["subject_type"],
                    "subject_id": row["subject_id"],
                    "subject_relation": row["subject_relation"],
                    "relation": row["relation"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "conditions": row["conditions"],
                }
                for row in cursor.fetchall()
            ]
