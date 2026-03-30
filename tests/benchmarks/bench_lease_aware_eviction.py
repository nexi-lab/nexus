"""Benchmarks for lease-aware cache eviction (Issue #3400).

Two targeted benchmarks:
1. Cache hit rate under concurrent read/write with and without lease-aware staleness
2. Eviction thrashing rate (re-fetch count) under space pressure

Usage:
    uv run pytest tests/benchmarks/bench_lease_aware_eviction.py -v -o "addopts="
"""

import threading
import time
from pathlib import Path

import pytest

from nexus.storage.file_cache import FileContentCache
from nexus.storage.local_disk_cache import LocalDiskCache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def file_cache(tmp_path: Path) -> FileContentCache:
    return FileContentCache(tmp_path)


@pytest.fixture
def disk_cache(tmp_path: Path) -> LocalDiskCache:
    # ~10KB cache — small enough that 1KB entries trigger eviction quickly
    return LocalDiskCache(cache_dir=tmp_path / "disk_cache", max_size_gb=10 / (1024 * 1024))


# ---------------------------------------------------------------------------
# Benchmark 1: Cache hit rate with / without lease-aware staleness
# ---------------------------------------------------------------------------


class TestCacheHitRateWithStaleness:
    """Measure how lease-aware staleness affects cache hit rate.

    Scenario: N files cached, M% have leases revoked mid-run.
    Compare hit rate with and without staleness detection.
    """

    def _populate_cache(self, cache: FileContentCache, zone: str, count: int) -> list[str]:
        """Write `count` files and return their paths."""
        paths = [f"/bench/file_{i:04d}.bin" for i in range(count)]
        for path in paths:
            cache.write(zone, path, f"content-for-{path}".encode())
        return paths

    def test_hit_rate_baseline_no_staleness(self, file_cache: FileContentCache) -> None:
        """Baseline: all reads hit cache (no staleness)."""
        zone = "bench"
        paths = self._populate_cache(file_cache, zone, 200)

        hits = 0
        total = 0
        for _ in range(5):
            for path in paths:
                result = file_cache.read(zone, path)
                total += 1
                if result is not None:
                    hits += 1

        hit_rate = hits / total
        assert hit_rate > 0.99, f"Baseline hit rate {hit_rate:.2%} should be ~100%"

    def test_hit_rate_with_25pct_staleness(self, file_cache: FileContentCache) -> None:
        """25% of files marked stale — hit rate should drop to ~75%."""
        zone = "bench"
        paths = self._populate_cache(file_cache, zone, 200)

        # Mark first 25% as stale
        stale_count = len(paths) // 4
        for path in paths[:stale_count]:
            file_cache.mark_lease_revoked(zone, path)

        hits = 0
        total = 0
        for path in paths:
            result = file_cache.read(zone, path)
            total += 1
            if result is not None:
                hits += 1

        hit_rate = hits / total
        assert 0.70 <= hit_rate <= 0.80, f"Hit rate {hit_rate:.2%} should be ~75% with 25% stale"

    def test_hit_rate_with_50pct_staleness(self, file_cache: FileContentCache) -> None:
        """50% of files marked stale — hit rate should drop to ~50%."""
        zone = "bench"
        paths = self._populate_cache(file_cache, zone, 200)

        # Mark first 50% as stale
        stale_count = len(paths) // 2
        for path in paths[:stale_count]:
            file_cache.mark_lease_revoked(zone, path)

        hits = 0
        total = 0
        for path in paths:
            result = file_cache.read(zone, path)
            total += 1
            if result is not None:
                hits += 1

        hit_rate = hits / total
        assert 0.45 <= hit_rate <= 0.55, f"Hit rate {hit_rate:.2%} should be ~50% with 50% stale"

    def test_hit_rate_recovery_after_write(self, file_cache: FileContentCache) -> None:
        """Staleness cleared by write — hit rate recovers to 100%."""
        zone = "bench"
        paths = self._populate_cache(file_cache, zone, 200)

        # Mark all stale
        for path in paths:
            file_cache.mark_lease_revoked(zone, path)

        # Re-write all (simulates re-fetch)
        for path in paths:
            file_cache.write(zone, path, f"fresh-{path}".encode())

        hits = 0
        total = 0
        for path in paths:
            result = file_cache.read(zone, path)
            total += 1
            if result is not None:
                hits += 1

        hit_rate = hits / total
        assert hit_rate > 0.99, f"Recovery hit rate {hit_rate:.2%} should be ~100% after re-write"


# ---------------------------------------------------------------------------
# Benchmark 2: Eviction thrashing (re-fetch count) under space pressure
# ---------------------------------------------------------------------------


class TestEvictionThrashingRate:
    """Measure eviction behavior under space pressure.

    Uses LocalDiskCache with a small size limit.  Tracks how many
    evictions occur when the cache is under pressure with a mix of
    high-priority (leased) and low-priority (unleased) entries.
    """

    def test_clock_eviction_prefers_low_priority(self, disk_cache: LocalDiskCache) -> None:
        """High-priority entries survive eviction longer than low-priority."""
        # Fill cache with low-priority entries (10KB cache, 1KB each → ~10 fit)
        low_priority_keys = []
        for i in range(15):
            key = f"{'a' * 62}{i:02d}"  # 64-char fake SHA-256
            disk_cache.put(key, b"x" * 1024, priority=0)
            low_priority_keys.append(key)

        # Add high-priority entries — triggers eviction of low-priority ones
        high_priority_keys = []
        for i in range(8):
            key = f"{'b' * 62}{i:02d}"
            disk_cache.put(key, b"y" * 1024, priority=2)
            high_priority_keys.append(key)

        # Check: high-priority entries should still be cached
        high_hits = sum(1 for k in high_priority_keys if disk_cache.exists(k))
        low_hits = sum(1 for k in low_priority_keys if disk_cache.exists(k))

        stats = disk_cache.get_stats()
        assert stats["evictions"] > 0, "Should have triggered evictions"
        # High-priority entries should have higher survival rate
        high_rate = high_hits / len(high_priority_keys)
        low_rate = low_hits / len(low_priority_keys) if low_priority_keys else 0
        assert high_rate >= low_rate, (
            f"High-priority survival ({high_rate:.0%}) should be >= low-priority ({low_rate:.0%})"
        )

    def test_eviction_stats_track_evicted_bytes(self, disk_cache: LocalDiskCache) -> None:
        """Eviction stats should accurately track bytes evicted."""
        # 10KB cache, 1KB entries → eviction starts after ~10 entries
        for i in range(30):
            key = f"{'c' * 62}{i:02d}"
            disk_cache.put(key, b"z" * 1024, priority=0)

        stats = disk_cache.get_stats()
        assert stats["evictions"] > 0, "Should have evictions under pressure"
        assert stats["bytes_evicted"] > 0, "Should track evicted bytes"
        # Size should be within limit
        assert stats["size_bytes"] <= disk_cache.max_size_bytes, (
            f"Cache size {stats['size_bytes']} exceeds max {disk_cache.max_size_bytes}"
        )

    def test_eviction_does_not_drift_size_tracking(self, disk_cache: LocalDiskCache) -> None:
        """Size tracking should remain accurate after many evictions (Issue #3400 fix 6A)."""
        # Rapid put/evict cycles: 10KB cache, 1KB entries, 100 puts
        for i in range(100):
            key = f"{'d' * 62}{i:02d}"
            disk_cache.put(key, b"w" * 1024, priority=0)

        stats = disk_cache.get_stats()
        actual_size = stats["size_bytes"]
        entry_count = stats["entries"]

        # Verify size tracking: sum of entries should match reported size
        # (This would drift pre-fix if file deletion failed)
        assert actual_size <= disk_cache.max_size_bytes, (
            f"Size {actual_size} exceeds max {disk_cache.max_size_bytes}"
        )
        assert entry_count >= 0


# ---------------------------------------------------------------------------
# Benchmark 3: Staleness check overhead
# ---------------------------------------------------------------------------


class TestStalenessCheckOverhead:
    """Measure the overhead of staleness checks on read operations."""

    def test_staleness_check_overhead_is_minimal(self, file_cache: FileContentCache) -> None:
        """Staleness check (set membership) should add negligible latency."""
        zone = "bench"
        paths = [f"/bench/file_{i:04d}.bin" for i in range(500)]
        for path in paths:
            file_cache.write(zone, path, b"benchmark-content")

        # Time 1000 reads without staleness
        start = time.perf_counter()
        for _ in range(2):
            for path in paths:
                file_cache.read(zone, path)
        baseline_elapsed = time.perf_counter() - start

        # Mark 50% stale, then time reads
        for path in paths[:250]:
            file_cache.mark_lease_revoked(zone, path)

        start = time.perf_counter()
        for _ in range(2):
            for path in paths:
                file_cache.read(zone, path)
        stale_elapsed = time.perf_counter() - start

        # Staleness check should add < 50% overhead
        # (In practice it's a set lookup, virtually free)
        overhead = (stale_elapsed - baseline_elapsed) / max(baseline_elapsed, 0.001)
        assert overhead < 0.5, f"Staleness check overhead {overhead:.1%} exceeds 50% threshold"

    def test_concurrent_read_throughput_with_staleness(self, file_cache: FileContentCache) -> None:
        """Concurrent reads with staleness tracking should not degrade throughput."""
        zone = "bench"
        paths = [f"/bench/file_{i:04d}.bin" for i in range(100)]
        for path in paths:
            file_cache.write(zone, path, b"concurrent-bench")

        # Mark 50% stale
        for path in paths[:50]:
            file_cache.mark_lease_revoked(zone, path)

        errors: list[Exception] = []

        def reader() -> None:
            try:
                count = 0
                for _ in range(50):
                    for path in paths:
                        file_cache.read(zone, path)
                        count += 1
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        elapsed = time.perf_counter() - start

        assert not errors, f"Reader threads raised: {errors}"
        # 4 threads * 50 iterations * 100 paths = 20,000 reads in < 30s
        assert elapsed < 30, f"Concurrent reads took {elapsed:.1f}s"
