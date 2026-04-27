"""Bulk Permission Checker — Multi-phase batch permission evaluation.

Extracts the rebac_check_bulk method from ReBACManager into a
focused class that orchestrates the multi-phase bulk checking pipeline:

Phase 0:   L1 in-memory cache lookup
Phase 0.5: Tiger Cache bitmap lookup
Phase 1:   Bulk tuple fetch (UNNEST/VALUES)
Phase 2:   In-memory graph computation (Rust or Python fallback)

Related: Issue #1459 Phase 15+, Performance optimization
"""

import logging
import threading
import time as time_module
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from nexus.bricks.rebac.domain import Entity
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.rebac_types import CROSS_ZONE_ALLOWED_RELATIONS

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache
    from nexus.bricks.rebac.domain import NamespaceConfig

logger = logging.getLogger(__name__)

SQLITE_ENTITY_CHUNK_SIZE = 400


class BulkPermissionChecker:
    """Multi-phase bulk permission checker.

    Orchestrates the pipeline: L1 cache -> Tiger Cache -> bulk fetch -> graph compute.

    Provides 100x reduction in DB queries for batch operations:
    - Before: N files * 15 queries/file = 300 queries
    - After: 1-2 queries to fetch all tuples + in-memory computation

    Args:
        engine: SQLAlchemy engine (for dialect detection and connections)
        get_namespace: Callable (entity_type) -> NamespaceConfig | None
        enforce_zone_isolation: Whether zone isolation is enabled
        l1_cache: L1 in-memory cache instance (or None)
        tiger_cache: Tiger bitmap cache instance (or None)
        compute_bulk_helper: Callable for in-memory graph computation
        rebac_check_single: Callable for fallback single-check
        cache_result: Callable to cache a single check result
        tuple_version: Current tuple version counter
    """

    def __init__(
        self,
        engine: "Engine",
        get_namespace: "Callable[[str], NamespaceConfig | None]",
        enforce_zone_isolation: bool,
        l1_cache: Any | None,
        tiger_cache: "TigerCache | None",
        compute_bulk_helper: "Callable[..., bool]",
        rebac_check_single: "Callable[..., bool]",
        cache_result: "Callable[..., None]",
        tuple_version: int,
        *,
        is_postgresql: bool = False,  # noqa: ARG002
    ) -> None:
        self._engine = engine
        self._get_namespace = get_namespace
        self._enforce_zone_isolation = enforce_zone_isolation
        self._l1_cache = l1_cache
        self._tiger_cache = tiger_cache
        self._compute_bulk_helper = compute_bulk_helper
        self._rebac_check_single = rebac_check_single
        self._cache_result = cache_result
        self._tuple_version = tuple_version

    def update_refs(
        self,
        l1_cache: Any | None = None,
        tiger_cache: "TigerCache | None" = None,
        tuple_version: int | None = None,
    ) -> None:
        """Update mutable references that may change after construction."""
        if l1_cache is not None:
            self._l1_cache = l1_cache
        if tiger_cache is not None:
            self._tiger_cache = tiger_cache
        if tuple_version is not None:
            self._tuple_version = tuple_version

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def check_bulk(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
    ) -> dict[tuple[tuple[str, str], str, tuple[str, str]], bool]:
        """Check permissions for multiple (subject, permission, object) tuples in batch.

        This is a performance optimization for list operations that need to check
        permissions on many objects. Instead of making N individual rebac_check() calls
        (each with 10-15 DB queries), this method:
        1. Fetches all relevant tuples in 1-2 queries
        2. Builds an in-memory permission graph
        3. Runs permission checks against the cached graph
        4. Returns all results in a single call

        Always uses cached (eventual) consistency.

        Args:
            checks: List of (subject, permission, object) tuples to check
            zone_id: Zone ID to scope all checks

        Returns:
            Dict mapping each check tuple to its result (True/False)
        """
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

        # Validate zone_id
        if not zone_id:
            import os

            is_production = (
                os.getenv("NEXUS_ENV") == "production" or os.getenv("ENVIRONMENT") == "production"
            )
            if is_production:
                raise ValueError("zone_id is required for bulk permission checks in production")
            else:
                logger.warning(
                    "rebac_check_bulk called without zone_id, defaulting to ROOT_ZONE_ID"
                )
                zone_id = ROOT_ZONE_ID

        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool] = {}
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]] = []

        # PHASE 0: L1 in-memory cache
        cache_misses = self._phase_l1_cache(checks, zone_id, results, bulk_start)
        if not cache_misses:
            return results

        # PHASE 0.5: Tiger Cache
        cache_misses = self._phase_tiger_cache(cache_misses, zone_id, results, bulk_start)
        if not cache_misses:
            return results

        logger.debug(f"Cache misses: {len(cache_misses)}, fetching tuples in bulk")

        # PHASE 1: Fetch all relevant tuples in bulk
        tuples_graph, _ancestor_paths = self._phase_fetch_tuples(cache_misses, zone_id)

        # PHASE 2: Compute permissions
        bulk_memo_cache: dict[tuple[str, str, str, str, str], bool] = {}
        memo_stats = {"hits": 0, "misses": 0, "max_depth": 0}

        logger.debug(
            f"Starting computation for {len(cache_misses)} cache misses with shared memo cache"
        )

        # Log schema verification for first check
        self._log_schema_verification(cache_misses)

        # Try Rust acceleration, fall back to Python
        rust_success = self._phase_rust_compute(
            cache_misses,
            tuples_graph,
            zone_id,
            results,
        )

        if not rust_success:
            self._phase_python_compute(
                cache_misses,
                tuples_graph,
                zone_id,
                results,
                bulk_memo_cache,
                memo_stats,
            )

        # Report cache statistics
        self._log_bulk_stats(memo_stats, bulk_memo_cache, results, bulk_start)

        return results

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def _phase_l1_cache(
        self,
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool],
        bulk_start: float,
    ) -> list[tuple[tuple[str, str], str, tuple[str, str]]]:
        """Phase 0: Check L1 in-memory cache. Returns remaining cache misses."""
        l1_start = time_module.perf_counter()
        l1_hits = 0
        l1_cache_enabled = self._l1_cache is not None
        logger.debug(f"[BULK-DEBUG] L1 cache enabled: {l1_cache_enabled}")

        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]] = []

        if l1_cache_enabled and self._l1_cache is not None:
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
                    "[BULK-PERF] All %d checks satisfied from L1 cache in %.1fms",
                    len(checks),
                    total_elapsed,
                )
        else:
            cache_misses = list(checks)
            logger.debug("[BULK-DEBUG] Skipping L1 cache (not available)")

        return cache_misses

    def _phase_tiger_cache(
        self,
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool],
        bulk_start: float,
    ) -> list[tuple[tuple[str, str], str, tuple[str, str]]]:
        """Phase 0.5: Tiger Cache bitmap lookup. Returns remaining misses."""
        if not self._tiger_cache:
            return cache_misses

        tiger_start = time_module.perf_counter()
        tiger_hits = 0
        tiger_remaining: list[tuple[tuple[str, str], str, tuple[str, str]]] = []

        tiger_checks = [
            (subject[0], subject[1], permission, obj[0], obj[1], zone_id)
            for subject, permission, obj in cache_misses
        ]

        tiger_results = self._tiger_cache.check_access_bulk(tiger_checks)

        for check in cache_misses:
            subject, permission, obj = check
            tiger_key = (subject[0], subject[1], permission, obj[0], obj[1], zone_id)
            tiger_result = tiger_results.get(tiger_key)

            if tiger_result is True:
                results[check] = True
                tiger_hits += 1
                if self._l1_cache is not None:
                    self._l1_cache.set(
                        subject[0], subject[1], permission, obj[0], obj[1], True, zone_id
                    )
            elif tiger_result is None:
                tiger_remaining.append(check)
            else:
                tiger_remaining.append(check)

        tiger_elapsed = (time_module.perf_counter() - tiger_start) * 1000
        logger.debug(
            f"[BULK-PERF] Tiger Cache BULK: {tiger_hits} hits, {len(tiger_remaining)} remaining in {tiger_elapsed:.1f}ms (2 queries)"
        )

        if not tiger_remaining:
            total_elapsed = (time_module.perf_counter() - bulk_start) * 1000
            logger.debug(
                "[BULK-PERF] All checks satisfied from L1 + Tiger cache in %.1fms",
                total_elapsed,
            )

        return tiger_remaining

    def _phase_fetch_tuples(
        self,
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Phase 1: Fetch all relevant tuples in bulk. Returns (tuples_graph, ancestor_paths)."""
        all_subjects: set[tuple[str, str]] = set()
        all_objects: set[tuple[str, str]] = set()
        for check in cache_misses:
            subject, permission, obj = check
            all_subjects.add(subject)
            all_objects.add(obj)

        # For file paths, compute ancestor paths for parent hierarchy
        file_paths = []
        for obj_type, obj_id in all_objects:
            if obj_type == "file" and "/" in obj_id:
                file_paths.append(obj_id)

        ancestor_paths: set[str] = set()
        for file_path in file_paths:
            parts = file_path.strip("/").split("/")
            for i in range(len(parts), 0, -1):
                ancestor = "/" + "/".join(parts[:i])
                ancestor_paths.add(ancestor)
            if file_path != "/":
                ancestor_paths.add("/")

        file_path_tuples = [("file", path) for path in ancestor_paths]
        all_objects.update(file_path_tuples)
        all_subjects.update(file_path_tuples)

        all_subjects_list = list(all_subjects)
        all_objects_list = list(all_objects)

        now_iso = datetime.now(UTC).isoformat()

        with self._engine.connect() as conn:
            # Single UNNEST/VALUES query for all entities
            all_entities = list(all_subjects | all_objects)

            logger.debug(
                f"[BULK-UNNEST] Fetching tuples for {len(all_entities)} entities in single query "
                f"(was: {len(all_subjects_list)} subjects + {len(all_objects_list)} objects in batches)"
            )

            fetch_start = time_module.perf_counter()
            tuples_graph = self._fetch_all_tuples_single_query(conn, all_entities, zone_id, now_iso)
            fetch_duration = (time_module.perf_counter() - fetch_start) * 1000

            logger.debug(
                f"[BULK-UNNEST] Fetched {len(tuples_graph)} tuples in {fetch_duration:.1f}ms"
            )

            # Cross-zone shares
            if self._enforce_zone_isolation and all_subjects_list:
                cross_zone_count = self._fetch_cross_zone_tuples(
                    conn, all_subjects_list, tuples_graph, now_iso
                )
                if cross_zone_count > 0:
                    logger.debug(
                        f"[BULK-UNNEST] Fetched {cross_zone_count} cross-zone tuples in single query"
                    )

            # Compute parent relationships in memory (needed for directory
            # inheritance regardless of zone isolation setting)
            if ancestor_paths:
                computed_parent_count = self._compute_parent_tuples(ancestor_paths, tuples_graph)
                if computed_parent_count > 0:
                    logger.debug(
                        f"Computed {computed_parent_count} parent tuples in memory for file hierarchy"
                    )

            logger.debug(
                f"Fetched {len(tuples_graph)} tuples in bulk for graph computation (includes parent hierarchy)"
            )

        return tuples_graph, ancestor_paths

    def _fetch_all_tuples_single_query(
        self,
        conn: Any,
        entities: list[tuple[str, str]],
        zone_id: str,
        now_iso: str,
    ) -> list[dict[str, Any]]:
        """Fetch all tuples for entities using UNNEST or chunked SQLite VALUES."""
        if not entities:
            return []

        is_postgresql = self._engine.dialect.name == "postgresql"
        if not is_postgresql and len(entities) > SQLITE_ENTITY_CHUNK_SIZE:
            rows: list[dict[str, Any]] = []
            seen: set[tuple[str, ...]] = set()
            for entity_chunk in self._chunked(entities, SQLITE_ENTITY_CHUNK_SIZE):
                chunk_rows = self._fetch_all_tuples_single_query(
                    conn,
                    entity_chunk,
                    zone_id,
                    now_iso,
                )
                for row in chunk_rows:
                    row_key = self._tuple_row_key(row)
                    if row_key not in seen:
                        seen.add(row_key)
                        rows.append(row)
            return rows

        entity_types = [e[0] for e in entities]
        entity_ids = [e[1] for e in entities]

        if is_postgresql:
            if self._enforce_zone_isolation:
                stmt = text("""
                    WITH entity_list AS (
                        SELECT unnest(CAST(:entity_types AS text[])) AS entity_type,
                               unnest(CAST(:entity_ids AS text[])) AS entity_id
                    )
                    SELECT DISTINCT
                        t.subject_type, t.subject_id, t.subject_relation,
                        t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                    FROM rebac_tuples t
                    WHERE t.zone_id = :zone_id
                      AND (t.expires_at IS NULL OR t.expires_at >= :now_iso)
                      AND (
                          EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                          OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                      )
                """)
                params: dict[str, Any] = {
                    "entity_types": entity_types,
                    "entity_ids": entity_ids,
                    "zone_id": zone_id,
                    "now_iso": now_iso,
                }
            else:
                stmt = text("""
                    WITH entity_list AS (
                        SELECT unnest(CAST(:entity_types AS text[])) AS entity_type,
                               unnest(CAST(:entity_ids AS text[])) AS entity_id
                    )
                    SELECT DISTINCT
                        t.subject_type, t.subject_id, t.subject_relation,
                        t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                    FROM rebac_tuples t
                    WHERE (t.expires_at IS NULL OR t.expires_at >= :now_iso)
                      AND (
                          EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                          OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                      )
                """)
                params = {
                    "entity_types": entity_types,
                    "entity_ids": entity_ids,
                    "now_iso": now_iso,
                }
        else:
            # SQLite: Use VALUES clause with named parameters
            values_parts = [f"(:type_{i}, :id_{i})" for i in range(len(entities))]
            values_str = ", ".join(values_parts)
            params = {}
            for i, (etype, eid) in enumerate(entities):
                params[f"type_{i}"] = etype
                params[f"id_{i}"] = eid

            if self._enforce_zone_isolation:
                params["zone_id"] = zone_id
                params["now_iso"] = now_iso
                stmt = text(f"""
                    WITH entity_list(entity_type, entity_id) AS (
                        VALUES {values_str}
                    )
                    SELECT DISTINCT
                        t.subject_type, t.subject_id, t.subject_relation,
                        t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                    FROM rebac_tuples t
                    WHERE t.zone_id = :zone_id
                      AND (t.expires_at IS NULL OR t.expires_at >= :now_iso)
                      AND (
                          EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                          OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                      )
                """)
            else:
                params["now_iso"] = now_iso
                stmt = text(f"""
                    WITH entity_list(entity_type, entity_id) AS (
                        VALUES {values_str}
                    )
                    SELECT DISTINCT
                        t.subject_type, t.subject_id, t.subject_relation,
                        t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                    FROM rebac_tuples t
                    WHERE (t.expires_at IS NULL OR t.expires_at >= :now_iso)
                      AND (
                          EXISTS (SELECT 1 FROM entity_list e WHERE t.subject_type = e.entity_type AND t.subject_id = e.entity_id)
                          OR EXISTS (SELECT 1 FROM entity_list e WHERE t.object_type = e.entity_type AND t.object_id = e.entity_id)
                      )
                """)

        result = conn.execute(stmt, params)
        return [self._row_to_tuple_dict(row) for row in result]

    def _fetch_cross_zone_tuples(
        self,
        conn: Any,
        all_subjects_list: list[tuple[str, str]],
        tuples_graph: list[dict[str, Any]],
        now_iso: str,
    ) -> int:
        """Fetch cross-zone share tuples and append to tuples_graph. Returns count added."""
        cross_zone_relations = list(CROSS_ZONE_ALLOWED_RELATIONS)
        is_postgresql = self._engine.dialect.name == "postgresql"

        if not is_postgresql and len(all_subjects_list) > SQLITE_ENTITY_CHUNK_SIZE:
            cross_zone_count = 0
            for subject_chunk in self._chunked(all_subjects_list, SQLITE_ENTITY_CHUNK_SIZE):
                cross_zone_count += self._fetch_cross_zone_tuples(
                    conn,
                    subject_chunk,
                    tuples_graph,
                    now_iso,
                )
            return cross_zone_count

        subject_types = [s[0] for s in all_subjects_list]
        subject_ids = [s[1] for s in all_subjects_list]

        if is_postgresql:
            stmt = text("""
                WITH subject_list AS (
                    SELECT unnest(CAST(:subject_types AS text[])) AS subject_type,
                           unnest(CAST(:subject_ids AS text[])) AS subject_id
                )
                SELECT DISTINCT
                    t.subject_type, t.subject_id, t.subject_relation,
                    t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                FROM rebac_tuples t
                WHERE t.relation = ANY(CAST(:relations AS text[]))
                  AND (t.expires_at IS NULL OR t.expires_at >= :now_iso)
                  AND EXISTS (
                      SELECT 1 FROM subject_list s
                      WHERE t.subject_type = s.subject_type AND t.subject_id = s.subject_id
                  )
            """)
            cross_zone_params: dict[str, Any] = {
                "subject_types": subject_types,
                "subject_ids": subject_ids,
                "relations": cross_zone_relations,
                "now_iso": now_iso,
            }
        else:
            # SQLite: Use VALUES clause with named parameters
            values_parts = [f"(:stype_{i}, :sid_{i})" for i in range(len(all_subjects_list))]
            values_str = ", ".join(values_parts)
            cross_zone_params = {}
            for i, (stype, sid) in enumerate(all_subjects_list):
                cross_zone_params[f"stype_{i}"] = stype
                cross_zone_params[f"sid_{i}"] = sid

            rel_parts = [f":rel_{i}" for i in range(len(cross_zone_relations))]
            rel_str = ", ".join(rel_parts)
            for i, rel in enumerate(cross_zone_relations):
                cross_zone_params[f"rel_{i}"] = rel

            cross_zone_params["now_iso"] = now_iso

            stmt = text(f"""
                WITH subject_list(subject_type, subject_id) AS (
                    VALUES {values_str}
                )
                SELECT DISTINCT
                    t.subject_type, t.subject_id, t.subject_relation,
                    t.relation, t.object_type, t.object_id, t.conditions, t.expires_at
                FROM rebac_tuples t
                WHERE t.relation IN ({rel_str})
                  AND (t.expires_at IS NULL OR t.expires_at >= :now_iso)
                  AND EXISTS (
                      SELECT 1 FROM subject_list s
                      WHERE t.subject_type = s.subject_type AND t.subject_id = s.subject_id
                  )
            """)

        result = conn.execute(stmt, cross_zone_params)
        cross_zone_count = 0
        for row in result:
            tuples_graph.append(self._row_to_tuple_dict(row))
            cross_zone_count += 1

        return cross_zone_count

    @staticmethod
    def _chunked(items: list[tuple[str, str]], chunk_size: int) -> Iterator[list[tuple[str, str]]]:
        """Yield fixed-size chunks sized for SQLite parameter limits."""
        for start in range(0, len(items), chunk_size):
            yield items[start : start + chunk_size]

    @staticmethod
    def _row_to_tuple_dict(row: Any) -> dict[str, Any]:
        return {
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "subject_relation": row.subject_relation,
            "relation": row.relation,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "conditions": row.conditions,
            "expires_at": row.expires_at,
        }

    @staticmethod
    def _tuple_row_key(row: dict[str, Any]) -> tuple[str, ...]:
        return (
            repr(row.get("subject_type")),
            repr(row.get("subject_id")),
            repr(row.get("subject_relation")),
            repr(row.get("relation")),
            repr(row.get("object_type")),
            repr(row.get("object_id")),
            repr(row.get("conditions")),
            repr(row.get("expires_at")),
        )

    def _compute_parent_tuples(
        self,
        ancestor_paths: set[str],
        tuples_graph: list[dict[str, Any]],
    ) -> int:
        """Compute parent relationships in memory from file paths. Returns count added."""
        computed_parent_count = 0
        for file_path in ancestor_paths:
            parent_path = str(PurePosixPath(file_path).parent)
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
        return computed_parent_count

    def _log_schema_verification(
        self,
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]],
    ) -> None:
        """Log first permission expansion for schema verification."""
        if not cache_misses:
            return
        first_check = cache_misses[0]
        subject, permission, obj = first_check
        obj_type = obj[0]
        namespace = self._get_namespace(obj_type)
        if namespace and namespace.has_permission(permission):
            usersets = namespace.get_permission_usersets(permission)
            logger.debug(
                f"[SCHEMA-VERIFY] Permission '{permission}' on '{obj_type}' expands to {len(usersets)} relations: {usersets}"
            )
            logger.debug(
                "[SCHEMA-VERIFY] Expected: 3 for hybrid schema (viewer, editor, owner) or 9 for flattened"
            )

    def _phase_rust_compute(
        self,
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]],
        tuples_graph: list[dict[str, Any]],
        zone_id: str,
        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool],
    ) -> bool:
        """Phase 2a: Try Rust acceleration. Returns True if successful."""
        from nexus.bricks.rebac.utils.fast import (
            check_permissions_bulk_with_fallback,
            is_rust_available,
        )

        rust_available = is_rust_available()
        logger.debug(
            "[BULK] cache_misses=%d, rust_available=%s, tuples_graph=%d",
            len(cache_misses),
            rust_available,
            len(tuples_graph),
        )

        if any(t.get("conditions") for t in tuples_graph):
            logger.debug("[BULK] Skipping Rust acceleration for conditioned tuples")
            return False

        if not (rust_available and len(cache_misses) >= 1):
            return False

        try:
            # Get all namespace configs
            object_types = {obj[0] for _, _, obj in cache_misses}
            namespace_configs: dict[str, Any] = {}
            for obj_type in object_types:
                ns = self._get_namespace(obj_type)
                if ns:
                    namespace_configs[obj_type] = ns.config

            if namespace_configs:
                sample_type = list(namespace_configs.keys())[0]
                sample_config = namespace_configs[sample_type]
                logger.debug(
                    f"[RUST-DEBUG] Sample namespace config for '{sample_type}': {str(sample_config)[:200]}"
                )

            import time

            rust_start = time.perf_counter()
            rust_results_dict = check_permissions_bulk_with_fallback(
                cache_misses,
                tuples_graph,
                namespace_configs,
                force_python=False,
                # Force a fresh Rust graph build per bulk-check call.
                # The Rust GRAPH_CACHE is process-global, so small integer
                # tuple_version values can collide across manager instances
                # and leak stale results between tests/call sites.
                tuple_version=time.time_ns(),
            )
            rust_elapsed = time.perf_counter() - rust_start
            per_check_us = (rust_elapsed / len(cache_misses)) * 1_000_000
            logger.debug(
                f"[RUST-TIMING] {len(cache_misses)} checks in {rust_elapsed * 1000:.1f}ms = {per_check_us:.1f}µs/check"
            )

            # Convert results and cache in L1
            avg_delta = rust_elapsed / len(cache_misses) if cache_misses else 0.0

            l1_cache_writes = 0
            for check in cache_misses:
                subject, permission, obj = check
                key = (subject[0], subject[1], permission, obj[0], obj[1])
                result = rust_results_dict.get(key, False)
                results[check] = result

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
                logger.debug(f"[RUST-PERF] Wrote {l1_cache_writes} results to L1 in-memory cache")

            # Write-through to Tiger Cache (Issue #935)
            self._write_through_tiger_cache(
                cache_misses,
                zone_id,
                lambda check: rust_results_dict.get(
                    (check[0][0], check[0][1], check[1], check[2][0], check[2][1]), False
                ),
            )

            logger.debug(
                "[BULK] Rust acceleration successful for %d checks",
                len(cache_misses),
            )
            return True

        except (RuntimeError, ValueError) as e:
            logger.debug("[BULK] Rust acceleration failed: %s, falling back to Python", e)
            return False

    def _phase_python_compute(
        self,
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]],
        tuples_graph: list[dict[str, Any]],
        zone_id: str,
        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool],
        bulk_memo_cache: dict[tuple[str, str, str, str, str], bool],
        memo_stats: dict[str, int],
    ) -> None:
        """Phase 2b: Python fallback computation."""
        logger.debug(
            "[BULK] Using Python fallback for %d checks (rust_available=False)",
            len(cache_misses),
        )
        for check in cache_misses:
            subject, permission, obj = check
            subject_entity = Entity(subject[0], subject[1])
            obj_entity = Entity(obj[0], obj[1])

            try:
                result = self._compute_bulk_helper(
                    subject_entity,
                    permission,
                    obj_entity,
                    zone_id,
                    tuples_graph,
                    bulk_memo_cache=bulk_memo_cache,
                    memo_stats=memo_stats,
                )
            except (RuntimeError, ValueError, OperationalError) as e:
                logger.warning(f"Bulk check failed for {check}, falling back: {e}")
                result = self._rebac_check_single(subject, permission, obj, zone_id=zone_id)

            results[check] = result

            self._cache_result(subject_entity, permission, obj_entity, result, zone_id)

        # Write-through to Tiger Cache after Python fallback
        self._write_through_tiger_cache(
            cache_misses,
            zone_id,
            lambda check: results.get(check, False),
        )

    def _write_through_tiger_cache(
        self,
        cache_misses: list[tuple[tuple[str, str], str, tuple[str, str]]],
        zone_id: str,
        get_result: Any,
    ) -> None:
        """Write positive results to Tiger Cache bitmaps (Issue #935)."""
        if not (self._tiger_cache and zone_id):
            return

        tiger_writes = 0
        tiger_updates: dict[tuple[str, str, str, str, str], set[int]] = {}

        # Issue #3192: Batch fetch int IDs first to avoid N individual DB round-trips
        positive_checks = [
            (subject, permission, obj)
            for subject, permission, obj in cache_misses
            if get_result((subject, permission, obj))
        ]
        if not positive_checks:
            return

        resource_keys = [(obj[0], obj[1]) for _, _, obj in positive_checks]
        int_id_map = self._tiger_cache._resource_map.get_int_ids_batch(resource_keys)

        # For resources not yet in the map, create them individually (rare path)
        for key in resource_keys:
            if key not in int_id_map:
                try:
                    int_id = self._tiger_cache._resource_map.get_or_create_int_id(key[0], key[1])
                    if int_id > 0:
                        int_id_map[key] = int_id
                except (KeyError, ValueError) as e:
                    logger.debug(f"[TIGER] Failed to get int_id for {key}: {e}")

        for subject, permission, obj in positive_checks:
            resource_key = (obj[0], obj[1])
            resource_int_id = int_id_map.get(resource_key, 0)
            if resource_int_id > 0:
                group_key = (subject[0], subject[1], permission, obj[0], zone_id)
                if group_key not in tiger_updates:
                    tiger_updates[group_key] = set()
                tiger_updates[group_key].add(resource_int_id)
                tiger_writes += 1

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
        tiger_cache = self._tiger_cache

        def _persist_updates(updates: dict, cache: Any) -> None:
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
                except (RuntimeError, OperationalError) as e:
                    logger.debug(f"[TIGER] Background persist failed: {e}")

        threading.Thread(
            target=_persist_updates,
            args=(tiger_updates.copy(), tiger_cache),
            daemon=True,
        ).start()

        if tiger_writes > 0:
            logger.debug(
                f"[TIGER] Write-through: {tiger_writes} positive results "
                f"to {len(tiger_updates)} Tiger Cache bitmaps (async persist started)"
            )

    def _log_bulk_stats(
        self,
        memo_stats: dict[str, int],
        bulk_memo_cache: dict[tuple[str, str, str, str, str], bool],
        results: dict[tuple[tuple[str, str], str, tuple[str, str]], bool],
        bulk_start: float,
    ) -> None:
        """Log bulk operation statistics."""
        total_accesses = memo_stats["hits"] + memo_stats["misses"]
        hit_rate = (memo_stats["hits"] / total_accesses * 100) if total_accesses > 0 else 0

        logger.debug(f"Bulk memo cache stats: {len(bulk_memo_cache)} unique checks stored")
        logger.debug(
            f"Cache performance: {memo_stats['hits']} hits + {memo_stats['misses']} misses = {total_accesses} total accesses"
        )
        logger.debug(f"Cache hit rate: {hit_rate:.1f}% ({memo_stats['hits']}/{total_accesses})")
        logger.debug(f"Max traversal depth reached: {memo_stats.get('max_depth', 0)}")

        total_elapsed = (time_module.perf_counter() - bulk_start) * 1000
        allowed_count = sum(1 for r in results.values() if r)
        denied_count = len(results) - allowed_count
        logger.debug(
            f"[BULK-PERF] rebac_check_bulk completed: {len(results)} results "
            f"({allowed_count} allowed, {denied_count} denied) in {total_elapsed:.1f}ms"
        )

        if self._l1_cache is not None:
            l1_stats_after = self._l1_cache.get_stats()
            logger.debug(f"[BULK-DEBUG] L1 cache stats after: {l1_stats_after}")
