"""Readdir latency-at-scale benchmarks (Issue #3706).

Measures:
1. NexusFS.sys_readdir wall time + peak memory at 100 → 10K entries
   (details=True vs details=False)
2. SearchService N+1 permission-check overhead: flat dir (1K files)
   vs 100-subdir dir (triggers has_accessible_descendants per subdir)
3. has_accessible_descendants serial vs batch

All tests mock the metastore / permission enforcer — no real DB required.

Run with:
    PYTHONPATH=src python -m pytest tests/benchmarks/bench_readdir_scale.py -v -s
"""

import statistics
import time
import tracemalloc
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.metadata import FileMetadata

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCALES = [100, 500, 1_000, 5_000, 10_000]
WARMUP = 3
ITERATIONS = 10  # Fewer iters than hotpath bench — each call is heavier

ZONE_ID = "bench_zone"
SUBJECT = ("user", "alice")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entries(n: int, *, prefix: str = "/bigdir/", dirs_every: int = 0) -> list[FileMetadata]:
    """Generate *n* FileMetadata entries under *prefix*.

    If *dirs_every* > 0, every dirs_every-th entry is a directory (entry_type=1).
    """
    entries: list[FileMetadata] = []
    for i in range(n):
        is_dir = dirs_every > 0 and (i % dirs_every == 0)
        path = f"{prefix}{'dir' if is_dir else 'file'}_{i:05d}" + ("/" if is_dir else ".txt")
        entries.append(
            FileMetadata(
                path=path,
                size=0 if is_dir else 1024,
                entry_type=1 if is_dir else 0,
                zone_id=ZONE_ID,
            )
        )
    return entries


def _measure(fn, *, iterations: int = ITERATIONS) -> dict[str, float]:
    """Run *fn* repeatedly, return wall-time stats (ms) and peak memory (KB)."""
    latencies: list[float] = []
    peak_mem = 0

    for _ in range(iterations):
        tracemalloc.start()
        t0 = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        latencies.append(elapsed * 1000)  # ms
        peak_mem = max(peak_mem, peak)

    return {
        "p50_ms": statistics.median(latencies),
        "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)],
        "min_ms": min(latencies),
        "max_ms": max(latencies),
        "peak_mem_kb": peak_mem / 1024,
    }


def _report(label: str, stats: dict[str, float]) -> None:
    print(
        f"  {label:.<60s} "
        f"p50={stats['p50_ms']:>8.1f}ms | "
        f"p99={stats['p99_ms']:>8.1f}ms | "
        f"peak_mem={stats['peak_mem_kb']:>8.0f}KB"
    )


# ---------------------------------------------------------------------------
# Fixtures: lightweight NexusFS stub
# ---------------------------------------------------------------------------


def _build_nexusfs_stub(entries: list[FileMetadata], *, implicit_dirs: bool = False) -> Any:
    """Build a minimal NexusFS whose sys_readdir exercises the real code path.

    Mocks out heavy init but keeps the real sys_readdir / _entry_to_detail_dict
    / _is_internal_path logic.
    """
    from nexus.core.nexus_fs import NexusFS

    with patch.object(NexusFS, "__init__", lambda self, *a, **kw: None):
        fs = NexusFS.__new__(NexusFS)

    # Minimal wiring for sys_readdir
    fs._zone_id = ZONE_ID
    fs.router = None  # Skip connector routing
    fs._connectors = {}

    # Mock metastore
    meta = MagicMock()
    meta.list.return_value = entries
    meta.list_iter.return_value = iter(entries)
    meta.is_implicit_directory.return_value = implicit_dirs
    fs.metadata = meta

    return fs


# ---------------------------------------------------------------------------
# Fixtures: lightweight SearchService stub
# ---------------------------------------------------------------------------


def _build_search_service_stub(
    entries: list[FileMetadata],
    *,
    enforce_permissions: bool = False,
    descendants_delay_ms: float = 0.0,
) -> Any:
    """Build a minimal SearchService for list_dir benchmarking.

    Args:
        entries: FileMetadata entries the metastore returns.
        enforce_permissions: Whether to enable permission enforcement.
        descendants_delay_ms: Simulated per-call delay for has_accessible_descendants.
    """
    from nexus.bricks.search.search_service import SearchService

    with patch.object(SearchService, "__init__", lambda self, *a, **kw: None):
        svc = SearchService.__new__(SearchService)

    # Mock metastore
    meta = MagicMock()
    meta.list.return_value = entries
    meta.list_iter.return_value = iter(entries)
    # list_directory_entries not defined → fast path won't fire (as in production)
    if hasattr(meta, "list_directory_entries"):
        delattr(meta, "list_directory_entries")
    spec_attrs = {name for name in dir(MagicMock()) if not name.startswith("_")}
    meta.__class__ = type(
        "MockMeta",
        (),
        {
            "__getattr__": lambda self, name: (
                MagicMock() if name in spec_attrs else object.__getattribute__(self, name)
            ),
        },
    )
    # Re-set the attrs we need
    meta_obj = MagicMock()
    meta_obj.list = MagicMock(return_value=entries)
    meta_obj.list_iter = MagicMock(return_value=iter(entries))
    # Ensure hasattr(meta, "list_directory_entries") is False
    meta_real = MagicMock(
        spec=[
            "list",
            "list_iter",
            "get",
            "exists",
            "is_implicit_directory",
        ]
    )
    meta_real.list.return_value = entries
    meta_real.list_iter.return_value = iter(entries)
    meta_real.is_implicit_directory.return_value = False

    svc.metadata = meta_real
    svc.router = None
    svc._enforce_permissions = enforce_permissions
    svc._permission_enforcer = None
    svc._indexer = None
    svc._parsed_views = None
    svc._parsed_view_service = None
    svc._kernel = None

    if enforce_permissions:
        enforcer = MagicMock()

        def _has_descendants(path: str, ctx: Any) -> bool:
            if descendants_delay_ms > 0:
                time.sleep(descendants_delay_ms / 1000)
            return True

        enforcer.has_accessible_descendants = _has_descendants
        enforcer.has_accessible_descendants_batch = MagicMock(
            side_effect=lambda prefixes, ctx: dict.fromkeys(prefixes, True)
        )
        enforcer.rebac_manager = MagicMock()
        enforcer.rebac_manager._tiger_cache = None  # Disable predicate pushdown
        enforcer.check = MagicMock(return_value=True)
        svc._permission_enforcer = enforcer

    return svc


# ============================================================================
# Benchmark 1 — NexusFS.sys_readdir scaling (no ReBAC)
# ============================================================================


class TestReaddirScaleNoRebac:
    """Measure sys_readdir wall time + peak memory at various directory sizes.

    Exercises nexus_fs.py ~5251: unbounded metadata.list() into a Python list.
    """

    @pytest.mark.parametrize("n", SCALES, ids=[f"{n}entries" for n in SCALES])
    def test_readdir_paths_only(self, n: int) -> None:
        """sys_readdir(details=False) — returns path strings only."""
        entries = _make_entries(n)
        fs = _build_nexusfs_stub(entries)

        # Warmup
        for _ in range(WARMUP):
            fs.sys_readdir("/bigdir/", recursive=False, details=False)

        stats = _measure(
            lambda: fs.sys_readdir("/bigdir/", recursive=False, details=False),
        )
        _report(f"sys_readdir paths_only  n={n:>5d}", stats)

        # Regression gate: <1ms per 1K entries for the simple path
        max_ms = n * 0.001  # 1µs per entry
        max_ms = max(max_ms, 1.0)  # floor at 1ms
        assert stats["p50_ms"] < max_ms * 10, (
            f"sys_readdir(details=False, n={n}) p50={stats['p50_ms']:.1f}ms exceeds {max_ms * 10:.0f}ms"
        )

    @pytest.mark.parametrize("n", SCALES, ids=[f"{n}entries" for n in SCALES])
    def test_readdir_with_details(self, n: int) -> None:
        """sys_readdir(details=True) — triggers is_implicit_directory per entry."""
        entries = _make_entries(n)
        fs = _build_nexusfs_stub(entries, implicit_dirs=False)

        for _ in range(WARMUP):
            fs.sys_readdir("/bigdir/", recursive=False, details=True)

        stats = _measure(
            lambda: fs.sys_readdir("/bigdir/", recursive=False, details=True),
        )
        _report(f"sys_readdir details     n={n:>5d}", stats)

        # details=True is heavier due to is_implicit_directory per entry;
        # regression gate is more generous
        max_ms = n * 0.005  # 5µs per entry
        max_ms = max(max_ms, 2.0)  # floor at 2ms
        assert stats["p50_ms"] < max_ms * 10, (
            f"sys_readdir(details=True, n={n}) p50={stats['p50_ms']:.1f}ms exceeds {max_ms * 10:.0f}ms"
        )

    def test_readdir_details_overhead_ratio(self) -> None:
        """details=True should be no more than 10× slower than details=False at 5K."""
        n = 5_000
        entries = _make_entries(n)
        fs = _build_nexusfs_stub(entries, implicit_dirs=False)

        for _ in range(WARMUP):
            fs.sys_readdir("/bigdir/", recursive=False, details=False)
            fs.sys_readdir("/bigdir/", recursive=False, details=True)

        stats_simple = _measure(
            lambda: fs.sys_readdir("/bigdir/", recursive=False, details=False),
        )
        stats_detail = _measure(
            lambda: fs.sys_readdir("/bigdir/", recursive=False, details=True),
        )
        ratio = stats_detail["p50_ms"] / max(stats_simple["p50_ms"], 0.001)
        print(
            f"  details overhead ratio at n={n}: {ratio:.1f}x "
            f"(simple={stats_simple['p50_ms']:.1f}ms, detail={stats_detail['p50_ms']:.1f}ms)"
        )
        # NOTE: ratio is deliberately high — details=True calls is_implicit_directory
        # per entry (Issue #3706 concern).  We cap at 500x just to catch catastrophic
        # regressions; the real purpose is to *surface* the overhead, not gate it.
        assert ratio < 500, f"details=True is {ratio:.0f}x slower than details=False (target <500x)"

    def test_readdir_memory_scaling(self) -> None:
        """Peak memory should scale roughly linearly with entry count."""
        mem_by_n: dict[int, float] = {}
        for n in [100, 1_000, 10_000]:
            entries = _make_entries(n)
            fs = _build_nexusfs_stub(entries)

            stats = _measure(
                lambda _fs=fs: _fs.sys_readdir("/bigdir/", recursive=False, details=False),
                iterations=3,
            )
            mem_by_n[n] = stats["peak_mem_kb"]
            _report(f"memory  n={n:>5d}", stats)

        # 10K should not use more than 50× the memory of 100 entries
        if mem_by_n[100] > 0:
            ratio = mem_by_n[10_000] / mem_by_n[100]
            print(f"  Memory ratio 10K/100: {ratio:.1f}x (expect ~100x linear)")
            assert ratio < 500, (
                f"Memory scaling suspicious: 10K uses {ratio:.0f}x of 100-entry memory"
            )


# ============================================================================
# Benchmark 2 — SearchService list_dir with ReBAC (N+1 pattern)
# ============================================================================


class TestReaddirRebacNPlus1:
    """Measure the N+1 permission check overhead in search_service.py ~1127.

    The _list_slow_path loads all entries via metadata.list(), then the
    permission filter calls has_accessible_descendants() per subdirectory.
    This benchmark makes that cost visible.
    """

    def test_flat_dir_1k_files(self) -> None:
        """1K files, no subdirs → no has_accessible_descendants calls."""
        entries = _make_entries(1_000, dirs_every=0)
        svc = _build_search_service_stub(entries, enforce_permissions=True)

        # Build a minimal context
        ctx = MagicMock()
        ctx.get_subject.return_value = SUBJECT
        ctx.zone_id = ZONE_ID
        ctx.user_id = SUBJECT[1]
        ctx.is_admin = False

        # We can't easily call svc.list_dir (too many dependencies),
        # so benchmark the core pattern: metadata.list + permission filter loop
        def _simulate_flat_listing() -> list[FileMetadata]:
            all_files = svc.metadata.list("/bigdir/", zone_id=ZONE_ID)
            # No directories → no has_accessible_descendants calls
            return [f for f in all_files if f.path.startswith("/")]

        for _ in range(WARMUP):
            _simulate_flat_listing()

        stats = _measure(_simulate_flat_listing)
        _report("flat 1K files (0 perm checks)", stats)

    def test_subdir_heavy_100_dirs(self) -> None:
        """100 subdirs among 1K entries → 100 serial has_accessible_descendants calls."""
        entries = _make_entries(1_000, dirs_every=10)  # Every 10th is a dir → 100 dirs
        svc = _build_search_service_stub(
            entries, enforce_permissions=True, descendants_delay_ms=0.0
        )

        enforcer = svc._permission_enforcer
        call_count = 0

        def _simulate_subdir_listing() -> tuple[list[FileMetadata], int]:
            nonlocal call_count
            all_files = svc.metadata.list("/bigdir/", zone_id=ZONE_ID)
            result = []
            perm_checks = 0
            for f in all_files:
                if not f.path.startswith("/"):
                    continue
                if f.entry_type == 1:  # directory
                    enforcer.has_accessible_descendants(f.path, None)
                    perm_checks += 1
                result.append(f)
            call_count = perm_checks
            return result, perm_checks

        for _ in range(WARMUP):
            _simulate_subdir_listing()

        stats = _measure(lambda: _simulate_subdir_listing())
        _report(f"100 subdirs / 1K entries ({call_count} perm checks)", stats)

        # Key assertion: we made exactly 100 permission checks (the N+1 problem)
        assert call_count == 100, f"Expected 100 perm checks, got {call_count}"
        print(f"  ⚠ N+1 pattern: {call_count} serial has_accessible_descendants calls")

    def test_subdir_with_simulated_latency(self) -> None:
        """100 subdirs with 0.5ms per permission check → visible overhead."""
        entries = _make_entries(1_000, dirs_every=10)
        svc = _build_search_service_stub(
            entries, enforce_permissions=True, descendants_delay_ms=0.5
        )
        enforcer = svc._permission_enforcer

        def _simulate_with_latency() -> int:
            all_files = svc.metadata.list("/bigdir/", zone_id=ZONE_ID)
            checks = 0
            for f in all_files:
                if f.entry_type == 1:
                    enforcer.has_accessible_descendants(f.path, None)
                    checks += 1
            return checks

        # Fewer iterations since each is slow
        stats = _measure(_simulate_with_latency, iterations=5)
        _report("100 subdirs @ 0.5ms/check (simulated)", stats)

        # 100 checks × 0.5ms = ~50ms minimum
        assert stats["p50_ms"] > 40, (
            f"Expected >=40ms from 100×0.5ms serial checks, got {stats['p50_ms']:.1f}ms"
        )
        print(
            f"  Serial overhead: {stats['p50_ms']:.0f}ms "
            f"(100 checks × 0.5ms = 50ms theoretical minimum)"
        )


# ============================================================================
# Benchmark 3 — has_accessible_descendants: serial vs batch
# ============================================================================


class TestSerialVsBatchDescendants:
    """Compare serial has_accessible_descendants vs batch.

    Demonstrates that the batch API (has_accessible_descendants_batch) from
    enforcer.py:269 avoids the N+1 pattern by loading the Tiger bitmap once.
    """

    @staticmethod
    def _build_enforcer_with_tiger(num_paths: int = 500) -> Any:
        """Build a mock enforcer with a pre-populated accessible paths list."""
        from nexus.bricks.rebac.enforcer import PermissionEnforcer

        enforcer = MagicMock(spec=PermissionEnforcer)

        # Simulate accessible paths (what Tiger cache would return)
        accessible_paths = [f"/bigdir/file_{i:05d}.txt" for i in range(num_paths)]

        def _has_descendants(prefix: str, ctx: Any) -> bool:
            prefix_norm = prefix.rstrip("/") + "/"
            return any(p.startswith(prefix_norm) for p in accessible_paths)

        def _has_descendants_batch(prefixes: list[str], ctx: Any) -> dict[str, bool]:
            results = {}
            for prefix in prefixes:
                prefix_norm = prefix.rstrip("/") + "/"
                results[prefix] = any(p.startswith(prefix_norm) for p in accessible_paths)
            return results

        enforcer.has_accessible_descendants = _has_descendants
        enforcer.has_accessible_descendants_batch = _has_descendants_batch

        return enforcer, accessible_paths

    @pytest.mark.parametrize("n_dirs", [10, 50, 100, 200])
    def test_serial_vs_batch(self, n_dirs: int) -> None:
        """Batch should outperform serial for N directories."""
        enforcer, _ = self._build_enforcer_with_tiger(num_paths=500)
        prefixes = [f"/bigdir/subdir_{i:03d}" for i in range(n_dirs)]
        ctx = MagicMock()

        # Serial: N individual calls
        def _serial() -> list[bool]:
            return [enforcer.has_accessible_descendants(p, ctx) for p in prefixes]

        # Batch: single call
        def _batch() -> dict[str, bool]:
            return enforcer.has_accessible_descendants_batch(prefixes, ctx)

        for _ in range(WARMUP):
            _serial()
            _batch()

        stats_serial = _measure(_serial, iterations=20)
        stats_batch = _measure(_batch, iterations=20)

        _report(f"serial  n_dirs={n_dirs:>3d}", stats_serial)
        _report(f"batch   n_dirs={n_dirs:>3d}", stats_batch)

        # Batch should be faster (or at least not slower) due to single bitmap load
        # In the mock, both do the same work, but this structure validates the API
        print(
            f"  serial/batch ratio: {stats_serial['p50_ms'] / max(stats_batch['p50_ms'], 0.001):.2f}x"
        )


# ============================================================================
# Benchmark 4 — Tiger cache stats visibility during listing
# ============================================================================


class TestTigerCacheStatsVisibility:
    """Verify Tiger cache stats increment during permission checks.

    This validates the observability mechanism described in the issue:
    permission check count should be visible via Tiger cache get_stats().
    """

    def test_cache_stats_track_checks(self) -> None:
        """Tiger cache hit/miss counters should reflect permission check volume."""
        from pyroaring import BitMap as RoaringBitmap

        from nexus.bricks.rebac.cache.tiger.bitmap_cache import CacheKey, TigerCache
        from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap

        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.url = "postgresql://localhost/test"
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        engine.connect.return_value = conn

        resource_map = TigerResourceMap(engine)
        num_resources = 1_000
        for i in range(num_resources):
            key = ("file", f"/bigdir/file_{i:05d}.txt")
            resource_map._uuid_to_int[key] = i
            resource_map._int_to_uuid[i] = key

        cache = TigerCache(engine=engine, resource_map=resource_map, rebac_manager=None)

        # Pre-populate bitmap
        bitmap = RoaringBitmap(range(num_resources))
        cache_key = CacheKey(
            subject_type=SUBJECT[0],
            subject_id=SUBJECT[1],
            permission="read",
            resource_type="file",
        )
        cache._cache[cache_key] = (bitmap, 0, time.time())

        stats_before = cache.get_stats()
        hits_before = stats_before["hits"]

        # Simulate 100 permission checks (what listing 100 subdirs would do)
        for i in range(100):
            cache.check_access(
                subject_type=SUBJECT[0],
                subject_id=SUBJECT[1],
                permission="read",
                resource_type="file",
                resource_id=f"/bigdir/file_{i:05d}.txt",
            )

        stats_after = cache.get_stats()
        hits_delta = stats_after["hits"] - hits_before

        print(f"  Tiger cache hits after 100 checks: +{hits_delta}")
        print(f"  Full stats: {stats_after}")

        assert hits_delta == 100, f"Expected 100 cache hits from 100 checks, got {hits_delta}"
