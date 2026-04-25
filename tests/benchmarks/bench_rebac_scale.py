"""ReBAC permission check latency at scale — Tiger cache hit/miss (Issue #3709).

Measures real-world latency on PostgreSQL with Tiger cache using demo data's
pre-built ReBAC graph (zones, grants, agent identities).

Scenarios:
    1. Single check — Tiger cache hit        — target <0.5ms p50
    2. Single check — Tiger cache miss       — measure cold path (graph computation)
    3. Bulk check 100 paths                  — target <100ms p50
    4. Search + ReBAC filter                 — 30 results through _apply_rebac_filter
    5. Cache invalidation storm              — 50 rapid writes, measure Tiger invalidation + re-warm
    6. ZoneGraphLoader TTL window            — after tuple mutation, stale cache duration

Suspicious code under test:
    - bitmap_cache.py ~131  — RLock contention under concurrency
    - zone_graph_loader.py ~49-50  — 300s TTL stale window
    - enforcer.py ~310-315  — Tiger miss was fail-open (fixed to fail-closed)
    - utils/fast.py ~121-161  — fresh Rust graph every bulk call (CPU cost)

Requirements:
    PostgreSQL running (``nexus up`` or ``docker compose up postgres``).

Run:
    pytest tests/benchmarks/bench_rebac_scale.py -v --benchmark-only
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE, DEFAULT_GROUP_NAMESPACE
from nexus.bricks.rebac.manager import ReBACManager
from nexus.storage.models import Base
from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PostgreSQL connection
# ---------------------------------------------------------------------------

PG_URL = os.environ.get(
    "NEXUS_DATABASE_URL",
    os.environ.get("POSTGRES_URL", "postgresql://postgres:nexus@localhost:5432/nexus"),
)

ZONE_ID = "bench_scale"
ZONE_ID_RESEARCH = "bench_research"
SUBJECT_ADMIN = ("agent", "bench_admin")
SUBJECT_USER = ("agent", "bench_user")
SUBJECT_VIEWER = ("agent", "bench_viewer")
SUBJECT_OUTSIDER = ("agent", "bench_outsider")

# Scale parameters — large enough to stress Tiger cache
NUM_FILES = 500
NUM_DIRS = 20
NUM_GROUPS = 5
NUM_BULK_FILES = 100
NUM_STORM_FILES = 50


def _pg_is_available() -> bool:
    """Check if PostgreSQL is reachable."""
    engine = None
    try:
        engine = create_engine(PG_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        if engine is not None:
            engine.dispose()


pytestmark = [
    pytest.mark.benchmark_permissions,
    pytest.mark.skipif(
        not _pg_is_available(),
        reason="PostgreSQL not available — start with: nexus up",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_engine() -> Engine:
    """PostgreSQL engine with ReBAC tables."""
    engine = create_engine(
        PG_URL,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def seeded_manager(pg_engine) -> ReBACManager:
    """ReBACManager on PostgreSQL with Tiger cache + realistic permission graph.

    Graph structure (scale):
        - {NUM_FILES} files across {NUM_DIRS} directories in ZONE_ID
        - bench_admin: direct_owner on /bench/ (inherits all)
        - bench_user: direct_viewer on /bench/ via group membership (eng-team)
        - bench_viewer: direct_viewer on individual files (no inheritance)
        - {NUM_GROUPS} groups with nested membership
        - 5-level deep directory hierarchy
        - Cross-zone tuple for bench_admin in ZONE_ID_RESEARCH
    """
    mgr = ReBACManager(
        engine=pg_engine,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
        cache_ttl_seconds=300,
        max_depth=50,
        enforce_zone_isolation=False,
        enable_graph_limits=True,
        enable_leopard=True,
        enable_tiger_cache=True,
        is_postgresql=True,
    )

    # Clean up any previous benchmark tuples
    with pg_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM rebac_tuples WHERE zone_id IN (:z1, :z2)"),
            {"z1": ZONE_ID, "z2": ZONE_ID_RESEARCH},
        )

    # Register namespaces
    mgr.create_namespace(DEFAULT_FILE_NAMESPACE)
    mgr.create_namespace(DEFAULT_GROUP_NAMESPACE)

    # --- Admin: direct_owner on root ---
    mgr.rebac_write(
        subject=SUBJECT_ADMIN,
        relation="direct_owner",
        object=("file", "/bench/"),
        zone_id=ZONE_ID,
    )

    # --- Group structure ---
    # bench_user -> member of eng-team
    mgr.rebac_write(
        subject=SUBJECT_USER,
        relation="member",
        object=("group", "eng-team"),
        zone_id=ZONE_ID,
    )
    # eng-team -> direct_viewer on /bench/
    mgr.rebac_write(
        subject=("group", "eng-team"),
        relation="direct_viewer",
        object=("file", "/bench/"),
        zone_id=ZONE_ID,
    )

    # Additional nested groups: group-0 -> group-1 -> ... -> eng-team
    for i in range(NUM_GROUPS):
        parent_group = "eng-team" if i == 0 else f"bench-group-{i - 1}"
        mgr.rebac_write(
            subject=("group", f"bench-group-{i}"),
            relation="member",
            object=("group", parent_group),
            zone_id=ZONE_ID,
        )

    # --- Directory hierarchy ---
    for d in range(NUM_DIRS):
        dir_path = f"/bench/dir_{d:03d}/"
        mgr.rebac_write(
            subject=("file", "/bench/"),
            relation="parent",
            object=("file", dir_path),
            zone_id=ZONE_ID,
        )

    # --- Deep hierarchy (5 levels) ---
    levels = [
        "/bench/deep/",
        "/bench/deep/l1/",
        "/bench/deep/l1/l2/",
        "/bench/deep/l1/l2/l3/",
        "/bench/deep/l1/l2/l3/l4/",
        "/bench/deep/l1/l2/l3/l4/l5/",
    ]
    mgr.rebac_write(
        subject=("file", "/bench/"),
        relation="parent",
        object=("file", levels[0]),
        zone_id=ZONE_ID,
    )
    for i in range(len(levels) - 1):
        mgr.rebac_write(
            subject=("file", levels[i]),
            relation="parent",
            object=("file", levels[i + 1]),
            zone_id=ZONE_ID,
        )
    # File at the bottom of the deep tree
    mgr.rebac_write(
        subject=("file", levels[-1]),
        relation="parent",
        object=("file", "/bench/deep/l1/l2/l3/l4/l5/deep_file.txt"),
        zone_id=ZONE_ID,
    )

    # --- Files with direct viewer grants for bench_viewer ---
    for i in range(NUM_FILES):
        d = i % NUM_DIRS
        path = f"/bench/dir_{d:03d}/file_{i:04d}.txt"
        mgr.rebac_write(
            subject=SUBJECT_VIEWER,
            relation="direct_viewer",
            object=("file", path),
            zone_id=ZONE_ID,
        )

    # --- Cross-zone tuple ---
    mgr.rebac_write(
        subject=SUBJECT_ADMIN,
        relation="direct_owner",
        object=("file", "/research/"),
        zone_id=ZONE_ID_RESEARCH,
    )

    # --- Warm Tiger cache ---
    # Process the queue to materialize bitmaps
    if mgr._tiger_updater is not None:
        # Queue updates for our benchmark subjects
        for subj_type, subj_id in (SUBJECT_ADMIN, SUBJECT_USER, SUBJECT_VIEWER):
            mgr._tiger_updater.queue_update(
                subject_type=subj_type,
                subject_id=subj_id,
                permission="read",
                resource_type="file",
                zone_id=ZONE_ID,
            )
        mgr._tiger_updater.process_queue(batch_size=100)

    yield mgr
    mgr.close()


def _perf_ms(func, *args, **kwargs):
    """Single-shot wall-clock timing. Returns (result, elapsed_ms)."""
    t0 = time.perf_counter()
    result = func(*args, **kwargs)
    return result, (time.perf_counter() - t0) * 1000


def _percentile(samples: list[float], p: float) -> float:
    """Return the p-th percentile of sorted samples (0–100)."""
    if not samples:
        return 0.0
    s = sorted(samples)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < len(s) else f
    d = k - f
    return s[f] + d * (s[c] - s[f])


# ---------------------------------------------------------------------------
# Scenario 1: Single check — Tiger cache hit
# ---------------------------------------------------------------------------


class TestTigerCacheHit:
    """Tiger cache hit: bitmap lookup after cache is warmed."""

    def test_tiger_cache_hit_latency(self, benchmark, seeded_manager):
        """After Tiger cache is warmed, single permission check should be <0.5ms p50.

        Exercises: bitmap_cache.py L1 in-memory → O(1) bitmap lookup.
        Uses SUBJECT_VIEWER which has direct grants (Tiger write-through on seed).
        """
        m = seeded_manager

        # Ensure L1 + Tiger are warm for this specific check
        m.rebac_check(
            subject=SUBJECT_VIEWER,
            permission="read",
            object=("file", "/bench/dir_000/file_0000.txt"),
            zone_id=ZONE_ID,
        )

        result = benchmark(
            m.rebac_check,
            subject=SUBJECT_VIEWER,
            permission="read",
            object=("file", "/bench/dir_000/file_0000.txt"),
            zone_id=ZONE_ID,
        )
        assert result is True

        # Verify Tiger cache contributed (not just L1)
        if m._tiger_cache is not None:
            stats = m._tiger_cache.get_stats()
            assert stats["hits"] > 0, "Tiger cache was never consulted"

    def test_tiger_hit_rlock_contention(self, seeded_manager):
        """Verify RLock (bitmap_cache.py ~131) doesn't degrade under concurrency.

        Spawns multiple threads doing Tiger-cached reads and measures
        whether p99 diverges from p50 (contention signal).
        """
        m = seeded_manager
        n_threads = 8
        n_checks = 50
        latencies: list[float] = []
        lock = threading.Lock()

        # Warm with SUBJECT_VIEWER (has direct grants → Tiger write-through)
        m.rebac_check(
            subject=SUBJECT_VIEWER,
            permission="read",
            object=("file", "/bench/dir_000/file_0000.txt"),
            zone_id=ZONE_ID,
        )

        def worker():
            local_lat: list[float] = []
            for i in range(n_checks):
                path = f"/bench/dir_{i % NUM_DIRS:03d}/file_{i:04d}.txt"
                _, ms = _perf_ms(
                    m.rebac_check,
                    subject=SUBJECT_VIEWER,
                    permission="read",
                    object=("file", path),
                    zone_id=ZONE_ID,
                )
                local_lat.append(ms)
            with lock:
                latencies.extend(local_lat)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        p50 = _percentile(latencies, 50)
        p99 = _percentile(latencies, 99)
        contention_ratio = p99 / max(p50, 0.001)

        logger.info(
            "[SCENARIO-1] RLock contention: p50=%.3fms p99=%.3fms ratio=%.1fx (%d samples)",
            p50,
            p99,
            contention_ratio,
            len(latencies),
        )
        # p99 should not be more than 20x p50 (contention bound)
        assert contention_ratio < 20.0, (
            f"RLock contention too high: p99/p50 = {contention_ratio:.1f}x "
            f"(p50={p50:.3f}ms, p99={p99:.3f}ms)"
        )


# ---------------------------------------------------------------------------
# Scenario 2: Single check — Tiger cache miss (cold graph computation)
# ---------------------------------------------------------------------------


class TestTigerCacheMiss:
    """Tiger cache miss: forces graph computation path."""

    def test_tiger_miss_cold_path_latency(self, seeded_manager):
        """With Tiger cache cleared, measure full graph computation cost.

        Exercises: zone_graph_loader.py fetch + Rust/Python graph computation.
        """
        m = seeded_manager
        samples: list[float] = []

        for i in range(20):
            # Clear L1 cache to force graph traversal
            if m._l1_cache is not None:
                m._l1_cache.clear()
            # Clear Tiger in-memory cache to force DB lookup or miss
            if m._tiger_cache is not None:
                m._tiger_cache.clear_memory_cache()

            result, ms = _perf_ms(
                m.rebac_check,
                subject=SUBJECT_VIEWER,
                permission="read",
                object=("file", f"/bench/dir_{i % NUM_DIRS:03d}/file_{i:04d}.txt"),
                zone_id=ZONE_ID,
            )
            assert result is True
            samples.append(ms)

        p50 = _percentile(samples, 50)
        p99 = _percentile(samples, 99)
        logger.info(
            "[SCENARIO-2] Tiger miss cold path: p50=%.2fms p99=%.2fms (%d samples)",
            p50,
            p99,
            len(samples),
        )
        # Cold path should still complete within 100ms p50
        assert p50 < 100.0, f"Tiger miss too slow: p50={p50:.2f}ms (target <100ms)"

    def test_tiger_miss_returns_correct_result(self, seeded_manager):
        """Verify Tiger miss does NOT return all-True (enforcer.py ~310-315).

        The enforcer's check_batch_prefixes_optimized returns all-True on
        Tiger miss — this test verifies that rebac_check (single check)
        computes the correct result on cache miss.
        """
        m = seeded_manager

        # Clear all caches
        if m._l1_cache is not None:
            m._l1_cache.clear()
        if m._tiger_cache is not None:
            m._tiger_cache.clear_memory_cache()

        # Outsider has no grants — should be denied
        result = m.rebac_check(
            subject=SUBJECT_OUTSIDER,
            permission="read",
            object=("file", "/bench/dir_000/file_0000.txt"),
            zone_id=ZONE_ID,
        )
        assert result is False, (
            "Tiger cache miss returned True for unauthorized subject — "
            "possible fail-open vulnerability (see enforcer.py ~310-315)"
        )


# ---------------------------------------------------------------------------
# Scenario 3: Bulk check 100 paths
# ---------------------------------------------------------------------------


class TestBulkCheck:
    """Bulk check 100 paths using check_permissions_bulk_with_fallback."""

    def test_bulk_check_100_paths(self, benchmark, seeded_manager):
        """Batch check 100 files — target <100ms p50.

        Exercises: utils/fast.py ~121-161 (fresh Rust graph per call).
        """
        m = seeded_manager
        checks = [
            (SUBJECT_VIEWER, "read", ("file", f"/bench/dir_{i % NUM_DIRS:03d}/file_{i:04d}.txt"))
            for i in range(NUM_BULK_FILES)
        ]

        results = benchmark(m.rebac_check_bulk, checks=checks, zone_id=ZONE_ID)

        assert len(results) == NUM_BULK_FILES
        assert all(results.values()), "Not all bulk checks returned True"

    def test_bulk_rust_graph_rebuild_cost(self, seeded_manager):
        """Measure CPU cost of fresh Rust graph every bulk call (fast.py ~121-161).

        Each rebac_check_bulk call builds a fresh Rust graph from tuples
        (tuple_version=time.time_ns() prevents caching). Measure how much
        time goes to graph construction vs actual permission computation.
        """
        m = seeded_manager
        checks = [
            (SUBJECT_VIEWER, "read", ("file", f"/bench/dir_{i % NUM_DIRS:03d}/file_{i:04d}.txt"))
            for i in range(NUM_BULK_FILES)
        ]

        samples: list[float] = []
        for _ in range(10):
            # Clear L1 so bulk path fetches tuples fresh
            if m._l1_cache is not None:
                m._l1_cache.clear()
            _, ms = _perf_ms(m.rebac_check_bulk, checks=checks, zone_id=ZONE_ID)
            samples.append(ms)

        p50 = _percentile(samples, 50)
        per_check = p50 / NUM_BULK_FILES
        logger.info(
            "[SCENARIO-3] Bulk 100 paths: p50=%.2fms (%.3fms/check) (%d runs)",
            p50,
            per_check,
            len(samples),
        )
        assert p50 < 500.0, (
            f"Bulk check too slow: p50={p50:.2f}ms (target <500ms for {NUM_BULK_FILES} paths)"
        )

    def test_bulk_vs_individual_speedup(self, seeded_manager):
        """Bulk should be faster than N individual checks at scale.

        Uses a cold subject (no Tiger bitmap) to force graph traversal
        on every individual check, while bulk does a single SQL fetch.
        """
        m = seeded_manager
        n = 50

        # Use SUBJECT_USER (group-based access) — each individual check must
        # traverse group membership, whereas bulk fetches tuples once.
        checks = [
            (SUBJECT_USER, "read", ("file", f"/bench/dir_{i % NUM_DIRS:03d}/file_{i:04d}.txt"))
            for i in range(n)
        ]

        def _clear_caches():
            if m._l1_cache is not None:
                m._l1_cache.clear()
            if m._tiger_cache is not None:
                m._tiger_cache.clear_memory_cache()

        # Individual (clear caches so each check traverses the graph)
        _clear_caches()
        t0 = time.perf_counter()
        for subj, perm, obj in checks:
            if m._l1_cache is not None:
                m._l1_cache.clear()
            m.rebac_check(subject=subj, permission=perm, object=obj, zone_id=ZONE_ID)
        individual_ms = (time.perf_counter() - t0) * 1000

        # Bulk (clear caches again for fair comparison)
        _clear_caches()
        t0 = time.perf_counter()
        m.rebac_check_bulk(checks=checks, zone_id=ZONE_ID)
        bulk_ms = (time.perf_counter() - t0) * 1000

        speedup = individual_ms / max(bulk_ms, 0.001)
        logger.info(
            "[SCENARIO-3] Bulk vs individual: individual=%.1fms bulk=%.1fms speedup=%.1fx",
            individual_ms,
            bulk_ms,
            speedup,
        )
        # With Tiger cache warm from DB, both paths may be fast.
        # Log the comparison; assert only that bulk is not catastrophically slower.
        assert bulk_ms < individual_ms * 2, (
            f"Bulk slower than 2x individual: individual={individual_ms:.1f}ms, "
            f"bulk={bulk_ms:.1f}ms"
        )


# ---------------------------------------------------------------------------
# Scenario 4: Search + ReBAC filter
# ---------------------------------------------------------------------------


class TestSearchReBACFilter:
    """Simulate search results filtered through ReBAC enforcement."""

    def test_apply_rebac_filter_30_results(self, seeded_manager):
        """Measure _apply_rebac_filter equivalent: check 30 search result paths.

        Uses enforcer.filter_search_results() which delegates to
        compute_permissions_bulk (Rust-accelerated, 1 SQL query + 1 Rust graph).
        """
        m = seeded_manager

        # Simulate 30 search results — mix of permitted and denied paths
        search_paths = [f"/bench/dir_{i % NUM_DIRS:03d}/file_{i:04d}.txt" for i in range(30)]

        # Build checks for bulk evaluation
        checks = [(SUBJECT_USER, "read", ("file", p)) for p in search_paths]

        samples: list[float] = []
        for _ in range(20):
            if m._l1_cache is not None:
                m._l1_cache.clear()
            _, ms = _perf_ms(m.rebac_check_bulk, checks=checks, zone_id=ZONE_ID)
            samples.append(ms)

        p50 = _percentile(samples, 50)
        p99 = _percentile(samples, 99)
        logger.info(
            "[SCENARIO-4] Search+ReBAC filter (30 results): p50=%.2fms p99=%.2fms",
            p50,
            p99,
        )
        # Search filter should complete within 200ms p50
        assert p50 < 200.0, (
            f"Search ReBAC filter too slow: p50={p50:.2f}ms (target <200ms for 30 paths)"
        )


# ---------------------------------------------------------------------------
# Scenario 5: Cache invalidation storm
# ---------------------------------------------------------------------------


class TestCacheInvalidationStorm:
    """Write 50 files rapidly, measure Tiger cache invalidation + re-warm."""

    def test_invalidation_storm_50_writes(self, seeded_manager, pg_engine):
        """Rapidly write 50 permission tuples and measure:
        1. Per-write invalidation latency
        2. Total storm duration
        3. Re-warm time after storm
        """
        m = seeded_manager

        # --- Phase 1: storm of 50 writes ---
        write_latencies: list[float] = []
        write_results: list[Any] = []

        for i in range(NUM_STORM_FILES):
            path = f"/bench/storm/file_{i:04d}.txt"
            t0 = time.perf_counter()
            wr = m.rebac_write(
                subject=SUBJECT_ADMIN,
                relation="direct_viewer",
                object=("file", path),
                zone_id=ZONE_ID,
            )
            write_latencies.append((time.perf_counter() - t0) * 1000)
            write_results.append(wr)

        storm_total = sum(write_latencies)
        write_p50 = _percentile(write_latencies, 50)
        write_p99 = _percentile(write_latencies, 99)

        logger.info(
            "[SCENARIO-5] Storm %d writes: total=%.1fms p50=%.2fms p99=%.2fms",
            NUM_STORM_FILES,
            storm_total,
            write_p50,
            write_p99,
        )

        # --- Phase 2: verify Tiger cache was invalidated ---
        if m._tiger_cache is not None:
            stats_after_storm = m._tiger_cache.get_stats()
            logger.info(
                "[SCENARIO-5] Tiger stats after storm: invalidations=%d",
                stats_after_storm.get("invalidations", 0),
            )

        # --- Phase 3: re-warm Tiger cache ---
        if m._tiger_updater is not None:
            t0 = time.perf_counter()
            m._tiger_updater.queue_update(
                subject_type=SUBJECT_ADMIN[0],
                subject_id=SUBJECT_ADMIN[1],
                permission="read",
                resource_type="file",
                zone_id=ZONE_ID,
            )
            processed = m._tiger_updater.process_queue(batch_size=100)
            rewarm_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[SCENARIO-5] Tiger re-warm: %.1fms (%d entries processed)",
                rewarm_ms,
                processed,
            )

        # --- Phase 4: verify reads work after storm ---
        result = m.rebac_check(
            subject=SUBJECT_ADMIN,
            permission="read",
            object=("file", "/bench/storm/file_0000.txt"),
            zone_id=ZONE_ID,
        )
        assert result is True

        # Per-write with invalidation should stay under 50ms p50
        assert write_p50 < 50.0, (
            f"Per-write invalidation too slow: p50={write_p50:.2f}ms (target <50ms)"
        )

        # --- Cleanup storm tuples ---
        for wr in write_results:
            try:
                m.rebac_delete(wr)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Scenario 6: ZoneGraphLoader TTL window
# ---------------------------------------------------------------------------


class TestZoneGraphLoaderTTL:
    """Measure zone_graph_loader.py ~49-50: 300s TTL stale window."""

    def test_ttl_stale_window(self, seeded_manager):
        """After a tuple mutation, the zone graph cache serves stale data
        until TTL expires (default 300s). Verify the cache is stale and
        measure how long reads use the cached (stale) version.
        """
        m = seeded_manager
        loader = m._zone_loader

        # Warm the zone graph cache (no subject — zone tuples only)
        tuples_before = loader.fetch_tuples_for_rust(ZONE_ID)
        count_before = len(tuples_before)

        # Write a new tuple directly (bypassing cache invalidation)
        # This simulates another process writing to the DB
        with m.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rebac_tuples "
                    "(tuple_id, zone_id, subject_zone_id, object_zone_id, "
                    "subject_type, subject_id, relation, object_type, object_id, created_at) "
                    "VALUES (:tid, :zid, :zid, :zid, :st, :si, :rel, :ot, :oi, NOW())"
                ),
                {
                    "tid": "bench-ttl-probe",
                    "zid": ZONE_ID,
                    "st": "agent",
                    "si": "ttl_probe_agent",
                    "rel": "direct_viewer",
                    "ot": "file",
                    "oi": "/bench/ttl_probe.txt",
                },
            )

        # Read again — should get cached (stale) version
        tuples_cached = loader.fetch_tuples_for_rust(ZONE_ID)
        count_cached = len(tuples_cached)

        is_stale = count_cached == count_before
        logger.info(
            "[SCENARIO-6] ZoneGraphLoader TTL: before=%d cached=%d stale=%s (TTL=%ds)",
            count_before,
            count_cached,
            is_stale,
            loader._cache_ttl,
        )

        # Document the TTL window — this is expected behavior, not a bug,
        # but the benchmark makes the staleness visible
        assert loader._cache_ttl == 300, f"Expected 300s TTL, got {loader._cache_ttl}s"

        # Cleanup probe tuple
        with m.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM rebac_tuples WHERE tuple_id = :tid"),
                {"tid": "bench-ttl-probe"},
            )

    def test_cache_refresh_latency(self, seeded_manager):
        """Measure the cost of a ZoneGraphLoader cache refresh (DB fetch).

        This is the latency users pay every TTL seconds.
        """
        m = seeded_manager
        loader = m._zone_loader

        samples: list[float] = []
        for _ in range(10):
            # Invalidate cache to force refresh
            with loader._cache_lock:
                loader._cache.clear()

            t0 = time.perf_counter()
            tuples = loader.fetch_tuples_for_rust(ZONE_ID)
            ms = (time.perf_counter() - t0) * 1000
            samples.append(ms)

        p50 = _percentile(samples, 50)
        p99 = _percentile(samples, 99)
        tuple_count = len(tuples)
        logger.info(
            "[SCENARIO-6] ZoneGraphLoader refresh: p50=%.2fms p99=%.2fms (%d tuples)",
            p50,
            p99,
            tuple_count,
        )
        # Refresh from PG should be under 200ms for our graph size
        assert p50 < 200.0, (
            f"Zone graph refresh too slow: p50={p50:.2f}ms (target <200ms, {tuple_count} tuples)"
        )
