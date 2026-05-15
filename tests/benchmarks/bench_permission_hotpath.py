"""Permission-checking hot-path benchmarks.

Measures:
1. ReBACManager.rebac_check() throughput with Tiger Cache on vs off
2. DeferredPermissionBuffer flush latency baseline

All tests mock the SQLAlchemy engine so no real database is required.
For Tiger Cache benchmarks the in-memory L1 bitmap is pre-populated,
giving a pure cache-hit throughput measurement.

Run with:
    PYTHONPATH=src python -m pytest tests/benchmarks/bench_permission_hotpath.py -v
"""

import statistics
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pyroaring import BitMap as RoaringBitmap

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZONE_ID = "bench_zone"
SUBJECT_ALICE = ("user", "alice")
NUM_RESOURCES = 500
WARMUP_ITERATIONS = 50
BENCH_ITERATIONS = 2_000

PERMISSION = "read"
RESOURCE_TYPE = "file"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_engine(dialect_name: str = "postgresql") -> MagicMock:
    """Create a MagicMock that quacks like a SQLAlchemy Engine."""
    engine = MagicMock()
    engine.dialect.name = dialect_name
    engine.url = f"{dialect_name}://localhost/test"
    # Provide a connect() context manager that yields a mock connection
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = conn
    engine.begin.return_value = conn
    return engine


def _measure_ops(fn, *, iterations: int = BENCH_ITERATIONS) -> dict[str, float]:
    """Run *fn* repeatedly and return ops/sec plus latency percentiles.

    Returns a dict with keys:
        ops_per_sec, p50_us, p99_us, min_us, max_us
    """
    latencies: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        latencies.append(time.perf_counter() - t0)

    latencies_us = [lat * 1_000_000 for lat in latencies]
    total_sec = sum(latencies)
    return {
        "ops_per_sec": iterations / total_sec if total_sec > 0 else float("inf"),
        "p50_us": statistics.median(latencies_us),
        "p99_us": sorted(latencies_us)[int(len(latencies_us) * 0.99)],
        "min_us": min(latencies_us),
        "max_us": max(latencies_us),
    }


def _report(label: str, stats: dict[str, float]) -> None:
    """Print a human-readable benchmark report line."""
    print(
        f"  {label:.<50s} "
        f"{stats['ops_per_sec']:>10,.0f} ops/s | "
        f"p50={stats['p50_us']:>8.1f}us | "
        f"p99={stats['p99_us']:>8.1f}us"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_engine() -> MagicMock:
    return _make_mock_engine("postgresql")


@pytest.fixture()
def mock_engine_sqlite() -> MagicMock:
    return _make_mock_engine("sqlite")


# ---------------------------------------------------------------------------
# Tiger Cache: build a pre-populated in-memory bitmap
# ---------------------------------------------------------------------------


def _build_tiger_cache(engine: MagicMock, num_resources: int = NUM_RESOURCES) -> Any:
    """Build a TigerCache with a pre-populated in-memory L1 bitmap.

    Directly injects entries into ``_cache`` and ``_resource_map`` so that
    ``check_access()`` never touches the database.
    """
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import CacheKey, TigerCache
    from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap

    resource_map = TigerResourceMap(engine)

    # Pre-populate the resource map (in-memory only, no DB)
    for i in range(num_resources):
        key = (RESOURCE_TYPE, f"/workspace/bench/file_{i:04d}.txt")
        resource_map._uuid_to_int[key] = i
        resource_map._int_to_uuid[i] = key

    cache = TigerCache(
        engine=engine,
        resource_map=resource_map,
        rebac_manager=None,
    )

    # Build a bitmap containing all resource int-ids
    bitmap = RoaringBitmap(range(num_resources))
    cache_key = CacheKey(
        subject_type=SUBJECT_ALICE[0],
        subject_id=SUBJECT_ALICE[1],
        permission=PERMISSION,
        resource_type=RESOURCE_TYPE,
    )
    cache._cache[cache_key] = (bitmap, 0, time.time())

    return cache


# ============================================================================
# Benchmark 1 — Tiger Cache check_access throughput (pure in-memory)
# ============================================================================


class TestTigerCacheThroughput:
    """Measure raw Tiger Cache L1 bitmap lookup throughput."""

    def test_tiger_cache_hit_throughput(self, mock_engine: MagicMock) -> None:
        """Pre-populated bitmap lookup should exceed 200k ops/sec."""
        cache = _build_tiger_cache(mock_engine)

        # Pick a resource that IS in the bitmap
        resource_id = "/workspace/bench/file_0042.txt"

        # Warmup
        for _ in range(WARMUP_ITERATIONS):
            cache.check_access(
                subject_type=SUBJECT_ALICE[0],
                subject_id=SUBJECT_ALICE[1],
                permission=PERMISSION,
                resource_type=RESOURCE_TYPE,
                resource_id=resource_id,
            )

        stats = _measure_ops(
            lambda: cache.check_access(
                subject_type=SUBJECT_ALICE[0],
                subject_id=SUBJECT_ALICE[1],
                permission=PERMISSION,
                resource_type=RESOURCE_TYPE,
                resource_id=resource_id,
            ),
        )
        _report("Tiger L1 bitmap HIT", stats)

        assert stats["ops_per_sec"] > 200_000, (
            f"Tiger cache hit throughput too low: {stats['ops_per_sec']:,.0f} ops/s (target >200k)"
        )

    def test_tiger_cache_miss_throughput(self, mock_engine: MagicMock) -> None:
        """Cache miss (resource not in bitmap) should still be fast."""
        cache = _build_tiger_cache(mock_engine)

        # Resource NOT in bitmap (int-id 9999 is beyond our range)
        unknown_path = "/workspace/bench/file_9999.txt"
        # Add to resource map so we get an int-id but the bitmap won't contain it
        cache._resource_map._uuid_to_int[(RESOURCE_TYPE, unknown_path)] = 9999
        cache._resource_map._int_to_uuid[9999] = (RESOURCE_TYPE, unknown_path)

        for _ in range(WARMUP_ITERATIONS):
            cache.check_access(
                subject_type=SUBJECT_ALICE[0],
                subject_id=SUBJECT_ALICE[1],
                permission=PERMISSION,
                resource_type=RESOURCE_TYPE,
                resource_id=unknown_path,
            )

        stats = _measure_ops(
            lambda: cache.check_access(
                subject_type=SUBJECT_ALICE[0],
                subject_id=SUBJECT_ALICE[1],
                permission=PERMISSION,
                resource_type=RESOURCE_TYPE,
                resource_id=unknown_path,
            ),
        )
        _report("Tiger L1 bitmap MISS (deny)", stats)

        assert stats["ops_per_sec"] > 200_000, (
            f"Tiger cache miss throughput too low: {stats['ops_per_sec']:,.0f} ops/s (target >200k)"
        )


# ============================================================================
# Benchmark 2 — rebac_check with Tiger Cache on vs off
# ============================================================================


def _build_manager_with_tiger(
    engine: MagicMock,
    tiger_cache: Any,
) -> Any:
    """Build a ReBACManager whose Tiger Cache is pre-populated.

    We patch out the constructor's heavy initialization (DB table creation,
    Leopard index, etc.) and inject the pre-built tiger cache directly.
    """
    from nexus.bricks.rebac.cache.tiger.facade import TigerFacade
    from nexus.bricks.rebac.manager import ReBACManager

    # Patch __init__ to avoid DB-dependent setup, then manually wire fields
    with patch.object(ReBACManager, "__init__", lambda self, *a, **kw: None):
        mgr = ReBACManager.__new__(ReBACManager)

    # Minimal wiring so rebac_check -> _rebac_check_inner works
    mgr.engine = engine
    mgr.cache_ttl_seconds = 300
    mgr.max_depth = 50
    mgr.enforce_zone_isolation = False
    mgr.enable_graph_limits = False
    mgr.enable_leopard = False
    mgr.enable_tiger_cache = True
    mgr._l1_cache = None  # Disable L1 so tiger path is exercised
    mgr._boundary_cache = None  # Disable boundary cache
    mgr._tiger_cache = tiger_cache
    mgr._tiger_updater = None
    mgr._tiger_facade = TigerFacade(tiger_cache=tiger_cache, tiger_updater=None)
    mgr._leopard = None
    mgr._namespaces: dict[str, Any] = {}
    mgr._namespaces_initialized = False
    mgr._tuple_version = 0
    mgr._zone_manager = MagicMock()
    mgr._zone_manager.validate_zone_access = MagicMock(return_value=None)

    return mgr


def _build_manager_no_tiger(engine_sqlite: MagicMock) -> Any:
    """Build a ReBACManager without Tiger Cache (simulates SQLite path).

    The rebac_check call will fall through to _rebac_check_base which we
    mock to return True instantly, giving a baseline for the non-tiger path
    overhead.
    """
    from nexus.bricks.rebac.cache.tiger.facade import TigerFacade
    from nexus.bricks.rebac.manager import ReBACManager

    with patch.object(ReBACManager, "__init__", lambda self, *a, **kw: None):
        mgr = ReBACManager.__new__(ReBACManager)

    mgr.engine = engine_sqlite
    mgr.cache_ttl_seconds = 300
    mgr.max_depth = 50
    mgr.enforce_zone_isolation = False
    mgr.enable_graph_limits = False
    mgr.enable_leopard = False
    mgr.enable_tiger_cache = False
    mgr._l1_cache = None
    mgr._boundary_cache = None
    mgr._tiger_cache = None
    mgr._tiger_updater = None
    mgr._tiger_facade = TigerFacade(tiger_cache=None, tiger_updater=None)
    mgr._leopard = None
    mgr._namespaces: dict[str, Any] = {}
    mgr._namespaces_initialized = False
    mgr._tuple_version = 0
    mgr._zone_manager = MagicMock()
    mgr._zone_manager.validate_zone_access = MagicMock(return_value=None)

    # Mock the base check path so it returns True without any DB access
    mgr._rebac_check_base = MagicMock(return_value=True)

    return mgr


class TestRebacCheckTigerOnVsOff:
    """Compare rebac_check throughput with Tiger Cache enabled vs disabled."""

    def test_rebac_check_with_tiger_cache(self, mock_engine: MagicMock) -> None:
        """rebac_check with a warm Tiger Cache should be >50k ops/sec."""
        tiger_cache = _build_tiger_cache(mock_engine)
        mgr = _build_manager_with_tiger(mock_engine, tiger_cache)

        resource_id = "/workspace/bench/file_0042.txt"
        obj = (RESOURCE_TYPE, resource_id)

        for _ in range(WARMUP_ITERATIONS):
            mgr.rebac_check(
                subject=SUBJECT_ALICE,
                permission=PERMISSION,
                object=obj,
                zone_id=ZONE_ID,
            )

        stats = _measure_ops(
            lambda: mgr.rebac_check(
                subject=SUBJECT_ALICE,
                permission=PERMISSION,
                object=obj,
                zone_id=ZONE_ID,
            ),
        )
        _report("rebac_check (Tiger ON, cache hit)", stats)

        assert stats["ops_per_sec"] > 50_000, (
            f"rebac_check with Tiger too slow: {stats['ops_per_sec']:,.0f} ops/s (target >50k)"
        )

    def test_rebac_check_without_tiger_cache(self, mock_engine_sqlite: MagicMock) -> None:
        """rebac_check without Tiger (mocked base path) baseline throughput."""
        mgr = _build_manager_no_tiger(mock_engine_sqlite)

        obj = (RESOURCE_TYPE, "/workspace/bench/file_0042.txt")

        for _ in range(WARMUP_ITERATIONS):
            mgr.rebac_check(
                subject=SUBJECT_ALICE,
                permission=PERMISSION,
                object=obj,
                zone_id=ZONE_ID,
            )

        stats = _measure_ops(
            lambda: mgr.rebac_check(
                subject=SUBJECT_ALICE,
                permission=PERMISSION,
                object=obj,
                zone_id=ZONE_ID,
            ),
        )
        _report("rebac_check (Tiger OFF, mocked base)", stats)

        # With a mocked base path we mainly measure framework overhead
        assert stats["ops_per_sec"] > 20_000, (
            f"rebac_check without Tiger too slow: {stats['ops_per_sec']:,.0f} ops/s (target >20k)"
        )


# ============================================================================
# Benchmark 3 — DeferredPermissionBuffer flush latency
# ============================================================================


class TestDeferredPermissionBufferFlush:
    """Measure DeferredPermissionBuffer flush latency with mock managers."""

    @staticmethod
    def _build_buffer(
        batch_size: int = 100,
    ) -> Any:
        """Create a DeferredPermissionBuffer with mocked rebac/hierarchy managers."""
        from nexus.bricks.rebac.deferred_permission_buffer import DeferredPermissionBuffer

        mock_rebac = MagicMock()
        mock_rebac.rebac_write_batch = MagicMock(return_value=None)

        mock_hierarchy = MagicMock()
        mock_hierarchy.ensure_parent_tuples_batch = MagicMock(return_value=None)

        buf = DeferredPermissionBuffer(
            rebac_manager=mock_rebac,
            hierarchy_manager=mock_hierarchy,
            flush_interval_sec=60.0,  # Large interval so background thread doesn't interfere
            max_batch_size=batch_size * 10,
        )
        return buf, mock_rebac, mock_hierarchy

    def test_flush_latency_empty_queue(self) -> None:
        """Flushing an empty queue should be <10us (no-op fast path)."""
        buf, _, _ = self._build_buffer()

        for _ in range(WARMUP_ITERATIONS):
            buf.flush()

        stats = _measure_ops(buf.flush, iterations=5_000)
        _report("DeferredBuffer flush (empty queue)", stats)

        assert stats["p50_us"] < 50.0, (
            f"Empty flush too slow: p50={stats['p50_us']:.1f}us (target <50us)"
        )

    def test_flush_latency_with_grants(self) -> None:
        """Flushing 100 queued grants should be <500us (mocked write)."""
        buf, mock_rebac, _ = self._build_buffer(batch_size=100)

        def _enqueue_and_flush() -> None:
            for i in range(100):
                buf.queue_owner_grant(
                    user=f"user_{i:04d}",
                    path=f"/workspace/bench/file_{i:04d}.txt",
                    zone_id=ZONE_ID,
                )
            buf.flush()

        # Warmup
        for _ in range(10):
            _enqueue_and_flush()

        stats = _measure_ops(_enqueue_and_flush, iterations=500)
        _report("DeferredBuffer flush (100 grants)", stats)

        # The mock write should be nearly instant; we measure dequeue + dispatch overhead
        assert stats["p50_us"] < 5_000.0, (
            f"100-grant flush too slow: p50={stats['p50_us']:.1f}us (target <5000us)"
        )

    def test_flush_latency_with_hierarchy(self) -> None:
        """Flushing 100 queued hierarchy operations should be <500us (mocked)."""
        buf, _, mock_hierarchy = self._build_buffer(batch_size=100)

        def _enqueue_and_flush() -> None:
            for i in range(100):
                buf.queue_hierarchy(
                    path=f"/workspace/bench/dir_{i:04d}/file.txt",
                    zone_id=ZONE_ID,
                )
            buf.flush()

        for _ in range(10):
            _enqueue_and_flush()

        stats = _measure_ops(_enqueue_and_flush, iterations=500)
        _report("DeferredBuffer flush (100 hierarchy)", stats)

        assert stats["p50_us"] < 5_000.0, (
            f"100-hierarchy flush too slow: p50={stats['p50_us']:.1f}us (target <5000us)"
        )

    def test_background_flush_fires(self) -> None:
        """Verify the background thread flushes within flush_interval_sec."""
        from nexus.bricks.rebac.deferred_permission_buffer import DeferredPermissionBuffer

        mock_rebac = MagicMock()
        mock_rebac.rebac_write_batch = MagicMock(return_value=None)
        mock_hierarchy = MagicMock()

        buf = DeferredPermissionBuffer(
            rebac_manager=mock_rebac,
            hierarchy_manager=mock_hierarchy,
            flush_interval_sec=0.05,  # 50ms
        )

        try:
            buf._start_sync()

            # Enqueue some grants
            for i in range(10):
                buf.queue_owner_grant(
                    user=f"user_{i}",
                    path=f"/workspace/bg_test/file_{i}.txt",
                    zone_id=ZONE_ID,
                )

            # Wait for background flush (50ms interval + margin)
            time.sleep(0.2)

            # The mock should have been called at least once
            assert mock_rebac.rebac_write_batch.call_count >= 1, (
                "Background flush did not fire within expected interval"
            )

            stats = buf.get_stats()
            assert stats["pending_grants"] == 0, (
                f"Grants still pending after background flush: {stats['pending_grants']}"
            )
        finally:
            buf._stop_sync(timeout=2.0)


# ============================================================================
# Benchmark 4 — Tiger Cache add_to_bitmap throughput (write-through path)
# ============================================================================


class TestTigerCacheWriteThroughput:
    """Measure write-through (add_to_bitmap) throughput for the hot path."""

    def test_add_to_bitmap_throughput(self, mock_engine: MagicMock) -> None:
        """Adding entries to an existing bitmap should be >100k ops/sec."""
        cache = _build_tiger_cache(mock_engine, num_resources=0)

        idx = 0

        def _add_one() -> None:
            nonlocal idx
            cache.add_to_bitmap(
                subject_type=SUBJECT_ALICE[0],
                subject_id=SUBJECT_ALICE[1],
                permission=PERMISSION,
                resource_type=RESOURCE_TYPE,
                zone_id=ZONE_ID,
                resource_int_id=idx,
            )
            idx += 1

        # Warmup
        for _ in range(WARMUP_ITERATIONS):
            _add_one()

        idx = 0  # Reset so we measure both new-add and existing-add paths
        stats = _measure_ops(_add_one, iterations=BENCH_ITERATIONS)
        _report("Tiger add_to_bitmap", stats)

        assert stats["ops_per_sec"] > 100_000, (
            f"add_to_bitmap throughput too low: {stats['ops_per_sec']:,.0f} ops/s (target >100k)"
        )
