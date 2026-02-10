#!/usr/bin/env python3
"""Benchmark Cache Warmup Performance (Issue #1076).

Measures the performance improvement from cache warmup:
1. Cold access latency (no cache)
2. Warm access latency (after warmup)
3. First-access improvement

Run: python benchmarks/performance/bench_cache_warmup.py
"""

import asyncio
import gc
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from nexus import NexusFS
from nexus.backends.local import LocalBackend
from nexus.cache.warmer import CacheWarmer, WarmupConfig
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


def create_test_files(nx: NexusFS, num_files: int, file_size: int = 1024) -> list[str]:
    """Create test files in NexusFS."""
    paths = []
    for i in range(num_files):
        path = f"/benchmark/warmup/file_{i:04d}.txt"
        content = os.urandom(file_size).hex()  # Random content
        nx.write(path, content)
        paths.append(path)
    return paths


def clear_caches(nx: NexusFS) -> None:
    """Clear all in-memory caches."""
    # Clear metadata cache
    if hasattr(nx, "metadata") and hasattr(nx.metadata, "_cache"):
        cache = nx.metadata._cache
        if cache:
            if hasattr(cache, "_path_cache"):
                cache._path_cache.clear()
            if hasattr(cache, "_list_cache"):
                cache._list_cache.clear()
            if hasattr(cache, "_exists_cache"):
                cache._exists_cache.clear()

    # Clear content cache
    if hasattr(nx, "backend") and hasattr(nx.backend, "content_cache"):
        cc = nx.backend.content_cache
        if cc and hasattr(cc, "clear"):
            cc.clear()

    # Force garbage collection
    gc.collect()


def benchmark_cold_access(nx: NexusFS, paths: list[str]) -> dict:
    """Benchmark file access with cold cache."""
    clear_caches(nx)

    # Measure individual access times
    times_exists = []
    times_read = []

    for path in paths:
        # Measure exists() - checks metadata cache
        start = time.perf_counter()
        _ = nx.exists(path)
        elapsed = time.perf_counter() - start
        times_exists.append(elapsed * 1000)  # ms

        # Measure read() - checks content cache
        start = time.perf_counter()
        _ = nx.read(path)
        elapsed = time.perf_counter() - start
        times_read.append(elapsed * 1000)  # ms

    return {
        "name": "Cold Cache",
        "exists_avg_ms": statistics.mean(times_exists),
        "exists_p50_ms": statistics.median(times_exists),
        "exists_p95_ms": sorted(times_exists)[int(len(times_exists) * 0.95)],
        "read_avg_ms": statistics.mean(times_read),
        "read_p50_ms": statistics.median(times_read),
        "read_p95_ms": sorted(times_read)[int(len(times_read) * 0.95)],
        "total_time_ms": sum(times_exists) + sum(times_read),
    }


def benchmark_warm_access(nx: NexusFS, paths: list[str]) -> dict:
    """Benchmark file access with warm cache (after warmup or previous access)."""
    # Cache should be warm from previous operations

    times_exists = []
    times_read = []

    for path in paths:
        start = time.perf_counter()
        _ = nx.exists(path)
        elapsed = time.perf_counter() - start
        times_exists.append(elapsed * 1000)

        start = time.perf_counter()
        _ = nx.read(path)
        elapsed = time.perf_counter() - start
        times_read.append(elapsed * 1000)

    return {
        "name": "Warm Cache",
        "exists_avg_ms": statistics.mean(times_exists),
        "exists_p50_ms": statistics.median(times_exists),
        "exists_p95_ms": sorted(times_exists)[int(len(times_exists) * 0.95)],
        "read_avg_ms": statistics.mean(times_read),
        "read_p50_ms": statistics.median(times_read),
        "read_p95_ms": sorted(times_read)[int(len(times_read) * 0.95)],
        "total_time_ms": sum(times_exists) + sum(times_read),
    }


async def benchmark_warmup_time(nx: NexusFS, num_files: int) -> dict:
    """Benchmark the warmup operation itself."""
    clear_caches(nx)

    config = WarmupConfig(
        max_files=num_files * 2,
        depth=3,
        include_content=False,
    )
    warmer = CacheWarmer(nexus_fs=nx, config=config)

    start = time.perf_counter()
    stats = await warmer.warmup_directory(
        path="/benchmark",
        depth=3,
        include_content=False,
        max_files=num_files * 2,
    )
    elapsed = time.perf_counter() - start

    return {
        "warmup_time_ms": elapsed * 1000,
        "files_warmed": stats.files_warmed,
        "metadata_warmed": stats.metadata_warmed,
        "files_per_second": stats.files_warmed / elapsed if elapsed > 0 else 0,
    }


def print_results(cold: dict, warm: dict, warmup: dict, num_files: int) -> None:
    """Print benchmark results."""
    print("\n" + "=" * 70)
    print(f"CACHE WARMUP BENCHMARK RESULTS ({num_files} files)")
    print("=" * 70)

    print("\n--- Warmup Operation ---")
    print(f"  Warmup time:        {warmup['warmup_time_ms']:.2f} ms")
    print(f"  Files warmed:       {warmup['files_warmed']}")
    print(f"  Throughput:         {warmup['files_per_second']:.0f} files/sec")

    print("\n--- exists() Latency ---")
    print(f"  {'Metric':<20} {'Cold':>12} {'Warm':>12} {'Improvement':>12}")
    print(f"  {'-' * 20} {'-' * 12} {'-' * 12} {'-' * 12}")

    for metric in ["exists_avg_ms", "exists_p50_ms", "exists_p95_ms"]:
        cold_val = cold[metric]
        warm_val = warm[metric]
        improvement = ((cold_val - warm_val) / cold_val * 100) if cold_val > 0 else 0
        label = metric.replace("exists_", "").replace("_ms", "")
        print(f"  {label:<20} {cold_val:>10.3f}ms {warm_val:>10.3f}ms {improvement:>10.1f}%")

    print("\n--- read() Latency ---")
    print(f"  {'Metric':<20} {'Cold':>12} {'Warm':>12} {'Improvement':>12}")
    print(f"  {'-' * 20} {'-' * 12} {'-' * 12} {'-' * 12}")

    for metric in ["read_avg_ms", "read_p50_ms", "read_p95_ms"]:
        cold_val = cold[metric]
        warm_val = warm[metric]
        improvement = ((cold_val - warm_val) / cold_val * 100) if cold_val > 0 else 0
        label = metric.replace("read_", "").replace("_ms", "")
        print(f"  {label:<20} {cold_val:>10.3f}ms {warm_val:>10.3f}ms {improvement:>10.1f}%")

    print("\n--- Total Time ---")
    cold_total = cold["total_time_ms"]
    warm_total = warm["total_time_ms"]
    improvement = ((cold_total - warm_total) / cold_total * 100) if cold_total > 0 else 0
    print(f"  Cold cache total:   {cold_total:.2f} ms")
    print(f"  Warm cache total:   {warm_total:.2f} ms")
    print(f"  Improvement:        {improvement:.1f}%")
    print(
        f"  Speedup:            {cold_total / warm_total:.1f}x"
        if warm_total > 0
        else "  Speedup: N/A"
    )

    print("\n--- Summary ---")
    print(f"  First-access improvement: {improvement:.0f}% faster with warmup")
    print(f"  Warmup cost:              {warmup['warmup_time_ms']:.0f}ms (one-time, background)")
    print(
        f"  Break-even:               {warmup['warmup_time_ms'] / (cold_total - warm_total):.1f} full scans"
        if cold_total > warm_total
        else "  Break-even: N/A"
    )

    print("=" * 70)


async def run_benchmark(num_files: int = 100, file_size: int = 1024) -> None:
    """Run the complete benchmark."""
    print(f"\nSetting up benchmark with {num_files} files ({file_size} bytes each)...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_path = Path(tmp_dir) / "storage"
        storage_path.mkdir()
        db_path = Path(tmp_dir) / "nexus.db"

        backend = LocalBackend(root_path=str(storage_path))
        nx = NexusFS(
            backend=backend,
            metadata_store=RaftMetadataStore.embedded(str(db_path).replace(".db", "-raft")),
            record_store=SQLAlchemyRecordStore(db_path=str(db_path)),
            enforce_permissions=False,
            enable_metadata_cache=True,
            cache_ttl_seconds=300,
        )

        try:
            # Create test files
            print(f"Creating {num_files} test files...")
            paths = create_test_files(nx, num_files, file_size)

            # Benchmark cold access
            print("Benchmarking cold cache access...")
            cold_results = benchmark_cold_access(nx, paths)

            # Run warmup
            print("Running cache warmup...")
            warmup_results = await benchmark_warmup_time(nx, num_files)

            # Benchmark warm access
            print("Benchmarking warm cache access...")
            warm_results = benchmark_warm_access(nx, paths)

            # Print results
            print_results(cold_results, warm_results, warmup_results, num_files)

        finally:
            nx.close()


def main():
    """Main entry point."""
    print("=" * 70)
    print("Cache Warmup Benchmark (Issue #1076)")
    print("=" * 70)

    # Run benchmarks with different file counts
    for num_files in [50, 200, 500]:
        asyncio.run(run_benchmark(num_files=num_files, file_size=1024))

    print("\nBenchmark complete!")


if __name__ == "__main__":
    main()
