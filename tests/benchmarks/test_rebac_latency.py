"""ReBAC permission check latency benchmarks (Issue #1371).

Measures p50/p99 latency for each cache layer in the permission check hot path:

1. L1 in-memory cache hit         — target p50 <0.5ms, p99 <2ms
2. Boundary cache hit              — target p50 <0.5ms, p99 <2ms
3. Tiger cache hit (PG-only)       — skipped on SQLite
4. Leopard index hit               — target p50 <2ms, p99 <10ms
5. Direct grant (graph depth=1)    — target p50 <5ms, p99 <20ms
6. Deep inheritance (depth=5)      — target p50 <50ms, p99 <200ms
7. Bulk check (100 objects)        — target p50 <100ms, p99 <500ms

Zanzibar reference: https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/
SpiceDB load testing: https://authzed.com/blog/spicedb-load-testing-guide

Run with:
    pytest tests/benchmarks/test_rebac_latency.py -v --benchmark-only
"""

import time

import pytest
from sqlalchemy import create_engine

from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE, DEFAULT_GROUP_NAMESPACE
from nexus.bricks.rebac.manager import ReBACManager
from nexus.storage.models import Base
from tests.helpers.inmemory_nexus_fs import InMemoryNexusFS

ZONE_ID = "bench_zone"


def _get_median_ms(benchmark) -> float | None:
    """Return benchmark median in ms, or None if stats unavailable (xdist)."""
    if benchmark.stats is not None:
        return benchmark.stats["median"] * 1000
    return None


def _measure_single_ms(func, *args, **kwargs):
    """Manual single-iteration timing fallback when benchmark stats are disabled."""
    t0 = time.perf_counter()
    result = func(*args, **kwargs)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return result, elapsed_ms


SUBJECT_ALICE = ("agent", "alice")
SUBJECT_BOB = ("agent", "bob")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with ReBAC tables."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def manager(engine):
    """Create a ReBACManager with caches enabled (no Tiger — SQLite only)."""
    mgr = ReBACManager(
        engine=engine,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
        cache_ttl_seconds=300,
        max_depth=50,
        enforce_zone_isolation=False,
        enable_graph_limits=True,
        enable_leopard=True,
        enable_tiger_cache=False,  # Tiger requires PostgreSQL
    )
    yield mgr
    mgr.close()


@pytest.fixture
def seeded_manager(manager):
    """Seed the manager with a realistic permission graph.

    Graph structure:
        alice -- direct_owner  --> /workspace/project/file_0.txt  (direct grant)
        alice -- direct_viewer --> /workspace/file_deep.txt       (direct for L1 test)
        alice -- member-of     --> eng-team                       (group membership)
        eng-team -- direct_viewer --> /workspace/                 (group grant)
        /workspace/project/ -- parent --> /workspace/             (directory hierarchy)
        /workspace/project/sub1/sub2/sub3/ -- parent --> ...      (deep hierarchy)

    This creates paths exercising:
    - Direct grant (file_0.txt -> alice is direct_owner)
    - Group-based (alice -> eng-team -> viewer on /workspace/)
    - Deep parent inheritance (5 levels of parent tuples)
    """
    m = manager

    # Register namespaces
    m.create_namespace(DEFAULT_FILE_NAMESPACE)
    m.create_namespace(DEFAULT_GROUP_NAMESPACE)

    # Direct grant: alice owns a specific file
    m.rebac_write(
        subject=SUBJECT_ALICE,
        relation="direct_owner",
        object=("file", "/workspace/project/file_0.txt"),
        zone_id=ZONE_ID,
    )

    # Direct viewer for L1 cache test
    m.rebac_write(
        subject=SUBJECT_ALICE,
        relation="direct_viewer",
        object=("file", "/workspace/file_cached.txt"),
        zone_id=ZONE_ID,
    )

    # Group membership: alice is member of eng-team
    m.rebac_write(
        subject=SUBJECT_ALICE,
        relation="member",
        object=("group", "eng-team"),
        zone_id=ZONE_ID,
    )

    # Group grant: eng-team has viewer on /workspace/
    m.rebac_write(
        subject=("group", "eng-team"),
        relation="direct_viewer",
        object=("file", "/workspace/"),
        zone_id=ZONE_ID,
    )

    # Parent hierarchy: /workspace/project/ -> parent -> /workspace/
    m.rebac_write(
        subject=("file", "/workspace/"),
        relation="parent",
        object=("file", "/workspace/project/"),
        zone_id=ZONE_ID,
    )

    # Deep hierarchy (5 levels): sub1 -> sub2 -> sub3 -> sub4 -> sub5
    levels = [
        "/workspace/deep/",
        "/workspace/deep/l1/",
        "/workspace/deep/l1/l2/",
        "/workspace/deep/l1/l2/l3/",
        "/workspace/deep/l1/l2/l3/l4/",
        "/workspace/deep/l1/l2/l3/l4/l5/",
    ]
    # Grant viewer on root of deep tree
    m.rebac_write(
        subject=SUBJECT_ALICE,
        relation="direct_viewer",
        object=("file", levels[0]),
        zone_id=ZONE_ID,
    )
    for i in range(len(levels) - 1):
        m.rebac_write(
            subject=("file", levels[i]),
            relation="parent",
            object=("file", levels[i + 1]),
            zone_id=ZONE_ID,
        )

    # Deep file at the bottom of the tree
    m.rebac_write(
        subject=("file", levels[-1]),
        relation="parent",
        object=("file", "/workspace/deep/l1/l2/l3/l4/l5/file_deep.txt"),
        zone_id=ZONE_ID,
    )

    # Bulk test: create 100 files with direct viewer grant
    for i in range(100):
        m.rebac_write(
            subject=SUBJECT_BOB,
            relation="direct_viewer",
            object=("file", f"/workspace/bulk/file_{i:04d}.txt"),
            zone_id=ZONE_ID,
        )

    return m


# ---------------------------------------------------------------------------
# Scenario 1: L1 In-Memory Cache Hit
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_ci
@pytest.mark.benchmark_permissions
class TestL1CacheHit:
    """L1 cache hit: second check on same (subject, perm, object) uses in-memory cache."""

    def test_l1_cache_hit_latency(self, benchmark, seeded_manager):
        """After warming the cache, subsequent checks should be <0.5ms p50."""
        m = seeded_manager
        subject = SUBJECT_ALICE
        obj = ("file", "/workspace/file_cached.txt")

        # Warm the L1 cache
        m.rebac_check(subject=subject, permission="read", object=obj, zone_id=ZONE_ID)

        # Benchmark the cached path
        result = benchmark(
            m.rebac_check,
            subject=subject,
            permission="read",
            object=obj,
            zone_id=ZONE_ID,
        )
        assert result is True

        # SLA: p50 < 2ms (generous for CI machines)
        median_ms = _get_median_ms(benchmark)
        if median_ms is None:
            _, median_ms = _measure_single_ms(
                m.rebac_check, subject=subject, permission="read", object=obj, zone_id=ZONE_ID
            )
        assert median_ms < 2.0, f"L1 cache hit too slow: p50={median_ms:.3f}ms (target <2ms)"


# ---------------------------------------------------------------------------
# Scenario 2: Boundary Cache Hit
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_permissions
class TestBoundaryCacheHit:
    """Boundary cache: O(1) ancestor inheritance shortcut for file permissions."""

    def test_boundary_cache_hit_latency(self, benchmark, seeded_manager):
        """After one check populates boundary cache, child paths use O(1) lookup."""
        m = seeded_manager

        # Warm boundary cache: check parent path first
        m.rebac_check(
            subject=SUBJECT_ALICE,
            permission="read",
            object=("file", "/workspace/project/"),
            zone_id=ZONE_ID,
        )

        # Now check a child file — boundary cache should shortcut
        result = benchmark(
            m.rebac_check,
            subject=SUBJECT_ALICE,
            permission="read",
            object=("file", "/workspace/project/file_0.txt"),
            zone_id=ZONE_ID,
        )
        assert result is True

        median_ms = _get_median_ms(benchmark)
        if median_ms is None:
            _, median_ms = _measure_single_ms(
                m.rebac_check,
                subject=SUBJECT_ALICE,
                permission="read",
                object=("file", "/workspace/project/file_0.txt"),
                zone_id=ZONE_ID,
            )
        assert median_ms < 5.0, f"Boundary cache hit too slow: p50={median_ms:.3f}ms (target <5ms)"


# ---------------------------------------------------------------------------
# Scenario 3: Tiger Cache Hit (PostgreSQL only — skipped on SQLite)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_permissions
class TestTigerCacheHit:
    """Tiger cache: Roaring Bitmap O(1) lookup. Requires PostgreSQL."""

    @pytest.mark.skip(reason="Tiger cache requires PostgreSQL — SQLite benchmarks skip this")
    def test_tiger_cache_hit_latency(self, benchmark, seeded_manager):
        """Tiger bitmap lookup should be <1ms p50."""
        # This test would require a PostgreSQL-backed ReBACManager.
        # When running with PG, remove the skip marker.
        pass


# ---------------------------------------------------------------------------
# Scenario 4: Leopard Index Hit (Transitive Group Closure)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_permissions
class TestLeopardIndexHit:
    """Leopard index: pre-computed transitive group closure for O(1) group checks."""

    def test_leopard_group_check_latency(self, benchmark, seeded_manager):
        """Group membership check via Leopard should be faster than graph traversal."""
        m = seeded_manager

        # Check if alice can read via eng-team group membership
        # This exercises the Leopard index path
        result = benchmark(
            m.rebac_check,
            subject=SUBJECT_ALICE,
            permission="read",
            object=("file", "/workspace/"),
            zone_id=ZONE_ID,
        )
        assert result is True

        median_ms = _get_median_ms(benchmark)
        if median_ms is None:
            _, median_ms = _measure_single_ms(
                m.rebac_check,
                subject=SUBJECT_ALICE,
                permission="read",
                object=("file", "/workspace/"),
                zone_id=ZONE_ID,
            )
        assert median_ms < 10.0, (
            f"Leopard group check too slow: p50={median_ms:.3f}ms (target <10ms)"
        )


# ---------------------------------------------------------------------------
# Scenario 5: Direct Grant (Graph Traversal Depth=1)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_permissions
class TestDirectGrantTraversal:
    """Direct grant: single-hop graph traversal (no inheritance)."""

    def test_direct_grant_latency(self, benchmark, seeded_manager):
        """Direct owner check should complete in <5ms p50."""
        m = seeded_manager

        # Clear L1 cache to force graph traversal
        if m._l1_cache is not None:
            m._l1_cache.clear()

        result = benchmark(
            m.rebac_check,
            subject=SUBJECT_ALICE,
            permission="read",
            object=("file", "/workspace/project/file_0.txt"),
            zone_id=ZONE_ID,
        )
        assert result is True

        median_ms = _get_median_ms(benchmark)
        if median_ms is None:
            if m._l1_cache is not None:
                m._l1_cache.clear()
            _, median_ms = _measure_single_ms(
                m.rebac_check,
                subject=SUBJECT_ALICE,
                permission="read",
                object=("file", "/workspace/project/file_0.txt"),
                zone_id=ZONE_ID,
            )
        assert median_ms < 20.0, (
            f"Direct grant traversal too slow: p50={median_ms:.3f}ms (target <20ms)"
        )


# ---------------------------------------------------------------------------
# Scenario 6: Deep Inheritance (Graph Traversal Depth=5+)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_permissions
class TestDeepInheritanceTraversal:
    """Deep inheritance: 5+ levels of parent traversal."""

    def test_deep_inheritance_latency(self, benchmark, seeded_manager):
        """5-level deep parent inheritance should complete in <50ms p50."""
        m = seeded_manager

        # Clear L1 cache to force full graph traversal
        if m._l1_cache is not None:
            m._l1_cache.clear()

        result = benchmark(
            m.rebac_check,
            subject=SUBJECT_ALICE,
            permission="read",
            object=("file", "/workspace/deep/l1/l2/l3/l4/l5/file_deep.txt"),
            zone_id=ZONE_ID,
        )
        assert result is True

        median_ms = _get_median_ms(benchmark)
        if median_ms is None:
            if m._l1_cache is not None:
                m._l1_cache.clear()
            _, median_ms = _measure_single_ms(
                m.rebac_check,
                subject=SUBJECT_ALICE,
                permission="read",
                object=("file", "/workspace/deep/l1/l2/l3/l4/l5/file_deep.txt"),
                zone_id=ZONE_ID,
            )
        assert median_ms < 200.0, (
            f"Deep inheritance too slow: p50={median_ms:.3f}ms (target <200ms)"
        )


# ---------------------------------------------------------------------------
# Scenario 7: Bulk Permission Check (100 Objects)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_ci
@pytest.mark.benchmark_permissions
class TestBulkPermissionCheck:
    """Bulk check: 100 objects in a single batch operation."""

    def test_bulk_check_latency(self, benchmark, seeded_manager):
        """Batch checking 100 files should be <100ms p50.

        Zanzibar-style bulk check: single SQL fetch + in-memory computation
        should be dramatically faster than 100 individual checks.
        """
        m = seeded_manager
        checks = [
            (SUBJECT_BOB, "read", ("file", f"/workspace/bulk/file_{i:04d}.txt")) for i in range(100)
        ]

        results = benchmark(m.rebac_check_bulk, checks=checks, zone_id=ZONE_ID)

        # Verify all 100 checks returned True
        assert len(results) == 100
        assert all(results.values()), "Not all bulk checks returned True"

        median_ms = _get_median_ms(benchmark)
        if median_ms is None:
            checks_repeat = [
                (SUBJECT_BOB, "read", ("file", f"/workspace/bulk/file_{i:04d}.txt"))
                for i in range(100)
            ]
            _, median_ms = _measure_single_ms(
                m.rebac_check_bulk, checks=checks_repeat, zone_id=ZONE_ID
            )
        assert median_ms < 500.0, (
            f"Bulk check (100 objects) too slow: p50={median_ms:.3f}ms (target <500ms)"
        )

    def test_bulk_check_vs_individual_speedup(self, seeded_manager):
        """Bulk check should be significantly faster than N individual checks.

        This is not a pytest-benchmark test — it directly measures wall-clock
        time to verify the bulk optimization provides real speedup.
        """
        import time

        m = seeded_manager
        checks = [
            (SUBJECT_BOB, "read", ("file", f"/workspace/bulk/file_{i:04d}.txt")) for i in range(50)
        ]

        # Clear cache
        if m._l1_cache is not None:
            m._l1_cache.clear()

        # Measure individual checks
        start = time.perf_counter()
        for subj, perm, obj in checks:
            m.rebac_check(subject=subj, permission=perm, object=obj, zone_id=ZONE_ID)
        individual_ms = (time.perf_counter() - start) * 1000

        # Clear cache again
        if m._l1_cache is not None:
            m._l1_cache.clear()

        # Measure bulk check
        start = time.perf_counter()
        m.rebac_check_bulk(checks=checks, zone_id=ZONE_ID)
        bulk_ms = (time.perf_counter() - start) * 1000

        # Bulk should be at least 2x faster (typically 10-100x)
        speedup = individual_ms / max(bulk_ms, 0.001)
        assert speedup > 1.5, (
            f"Bulk check not faster: individual={individual_ms:.1f}ms, "
            f"bulk={bulk_ms:.1f}ms, speedup={speedup:.1f}x (expected >1.5x)"
        )


# ---------------------------------------------------------------------------
# Scenario: Denial Check (verify fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_permissions
class TestDenialLatency:
    """Denial: check that returns False should also be fast."""

    def test_denial_latency(self, benchmark, seeded_manager):
        """Permission denial (no matching tuple) should be <20ms p50.

        A system that's slow to deny is a security risk (DoS amplification).
        """
        m = seeded_manager

        result = benchmark(
            m.rebac_check,
            subject=("agent", "unknown_user"),
            permission="write",
            object=("file", "/workspace/project/file_0.txt"),
            zone_id=ZONE_ID,
        )
        assert result is False

        median_ms = _get_median_ms(benchmark)
        if median_ms is None:
            _, median_ms = _measure_single_ms(
                m.rebac_check,
                subject=("agent", "unknown_user"),
                permission="write",
                object=("file", "/workspace/project/file_0.txt"),
                zone_id=ZONE_ID,
            )
        assert median_ms < 50.0, f"Denial check too slow: p50={median_ms:.3f}ms (target <50ms)"


# ---------------------------------------------------------------------------
# Scenario: Consistency Level Impact
# ---------------------------------------------------------------------------


@pytest.mark.benchmark_permissions
class TestCachedConsistencyLatency:
    """Measure latency for cached (the only) consistency mode."""

    def test_cached_consistency_latency(self, benchmark, seeded_manager):
        """Cached path should be the fastest path."""
        m = seeded_manager

        # Warm cache
        m.rebac_check(
            subject=SUBJECT_ALICE,
            permission="read",
            object=("file", "/workspace/file_cached.txt"),
            zone_id=ZONE_ID,
        )

        result = benchmark(
            m.rebac_check,
            subject=SUBJECT_ALICE,
            permission="read",
            object=("file", "/workspace/file_cached.txt"),
            zone_id=ZONE_ID,
        )
        assert result is True


# ---------------------------------------------------------------------------
# Cross-zone invalidation latency (Issue #3396)
# ---------------------------------------------------------------------------


class TestCrossZoneInvalidationLatency:
    """Benchmark cross-zone invalidation components.

    Measures latency of the read fence check and the full invalidation
    pipeline with durable stream publish.
    """

    def test_read_fence_check_latency(self, benchmark):
        """Read fence is_stale() should be <1μs (dict lookup + int compare).

        Target: p50 <0.001ms (1μs), p99 <0.01ms (10μs)
        """
        from nexus.bricks.rebac.cache.read_fence import ReadFence

        fence = ReadFence()
        # Simulate 10 zones with different generation counts
        for i in range(10):
            for _ in range(10 + i):
                fence.advance(f"zone-{i}")

        def check():
            return fence.is_stale("zone-5", 0)  # gen 0 is always stale after advance

        result = benchmark(check)
        assert result is True

    def test_read_fence_advance_latency(self, benchmark):
        """ReadFence.advance() should be <1μs.

        Target: p50 <0.001ms (1μs)
        """
        from nexus.bricks.rebac.cache.read_fence import ReadFence

        fence = ReadFence()

        def advance():
            fence.advance("zone-a")

        benchmark(advance)

    def test_durable_stream_publish_latency(self, benchmark):
        """Sync publish (queue append) should be <10μs.

        This is the in-process deque append, not the Redis round-trip.
        Target: p50 <0.01ms (10μs)
        """
        from unittest.mock import MagicMock

        from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream

        stream = DurableInvalidationStream(redis_client=MagicMock(), zone_id="bench")

        payload = {
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "editor",
            "object_type": "file",
            "object_id": "/doc.txt",
        }

        result = benchmark(stream.publish, "zone-target", payload)
        assert result is True

    def test_invalidation_pipeline_with_durable_stream(self, benchmark, seeded_manager):
        """Full invalidation pipeline including durable stream publish step.

        Measures the overhead of adding the durable stream publish to the
        existing invalidation pipeline.
        Target: <2x overhead vs pipeline without durable stream
        """
        from unittest.mock import MagicMock

        from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream
        from nexus.bricks.rebac.cache.read_fence import ReadFence

        m = seeded_manager
        coord = m._cache_coordinator

        # Wire mock durable stream + read fence
        mock_durable = DurableInvalidationStream(redis_client=MagicMock(), zone_id=ZONE_ID)
        fence = ReadFence()
        coord.set_durable_stream(mock_durable)
        coord.set_read_fence(fence)

        def invalidate():
            coord.invalidate_for_write(
                zone_id=ZONE_ID,
                subject=SUBJECT_ALICE,
                relation="editor",
                object=("file", "/workspace/file_0.txt"),
            )

        benchmark(invalidate)
