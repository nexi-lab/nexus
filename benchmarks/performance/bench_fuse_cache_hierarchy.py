#!/usr/bin/env python3
"""Benchmark FUSE cache hierarchy (Issue #1072).

Simulates the full FUSE read path:
    L1: In-memory cache (FUSECacheManager)
    L2: LocalDiskCache (SSD) - NEW
    L3: Network/Backend

Compares:
1. Without L2 (current): L1 miss → Network
2. With L2 (new): L1 miss → L2 hit → Fast SSD read

Run: python benchmarks/performance/bench_fuse_cache_hierarchy.py
"""

import hashlib
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def simulate_network_latency(base_ms: float = 20.0, jitter_ms: float = 5.0) -> float:
    """Simulate network latency with jitter."""
    import random

    return base_ms + random.uniform(-jitter_ms, jitter_ms)


def benchmark_without_l2(num_files: int = 100, file_size: int = 4096) -> dict:
    """Benchmark WITHOUT LocalDiskCache (current behavior).

    L1 miss → Network read (simulated 20ms latency)
    """
    print("\n" + "=" * 70)
    print("WITHOUT L2 CACHE (Current Behavior)")
    print("=" * 70)
    print("  L1 miss → Network read (simulated 20ms latency)")

    # Simulate L1 cache (in-memory, limited size)
    l1_cache: dict[str, bytes] = {}
    l1_max_size = 10  # Only 10 entries

    # Generate test files
    files = [(content_hash(os.urandom(file_size)), os.urandom(file_size)) for _ in range(num_files)]

    times = []
    l1_hits = 0
    network_reads = 0

    for hash_val, content in files:
        start = time.perf_counter()

        # Check L1 cache
        if hash_val in l1_cache:
            result = l1_cache[hash_val]
            l1_hits += 1
        else:
            # L1 miss → Network read
            time.sleep(simulate_network_latency() / 1000)  # Simulate network
            result = content
            network_reads += 1

            # Populate L1 (with eviction)
            if len(l1_cache) >= l1_max_size:
                l1_cache.pop(next(iter(l1_cache)))
            l1_cache[hash_val] = result

        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

        # Second access (should hit L1)
        start = time.perf_counter()
        if hash_val in l1_cache:
            result = l1_cache[hash_val]
            l1_hits += 1
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

    print(f"\n  Results ({num_files} files, {file_size} bytes each):")
    print(f"    L1 hits:      {l1_hits}")
    print(f"    Network reads: {network_reads}")
    print(f"    Avg latency:  {statistics.mean(times):.2f}ms")
    print(f"    P95 latency:  {sorted(times)[int(len(times) * 0.95)]:.2f}ms")

    return {
        "name": "Without L2",
        "l1_hits": l1_hits,
        "network_reads": network_reads,
        "avg_ms": statistics.mean(times),
        "p95_ms": sorted(times)[int(len(times) * 0.95)],
        "total_ms": sum(times),
    }


def benchmark_with_l2(num_files: int = 100, file_size: int = 4096) -> dict:
    """Benchmark WITH LocalDiskCache (new behavior).

    L1 miss → L2 hit → Fast SSD read (0.02ms)
    """
    from nexus.storage.local_disk_cache import LocalDiskCache

    print("\n" + "=" * 70)
    print("WITH L2 CACHE (LocalDiskCache - Issue #1072)")
    print("=" * 70)
    print("  L1 miss → L2 hit → Fast SSD read (0.02ms)")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Initialize L2 cache
        l2_cache = LocalDiskCache(cache_dir=tmpdir, max_size_gb=1.0)

        # Simulate L1 cache (in-memory, limited size)
        l1_cache: dict[str, bytes] = {}
        l1_max_size = 10  # Only 10 entries

        # Generate test files
        files = [
            (content_hash(os.urandom(file_size)), os.urandom(file_size)) for _ in range(num_files)
        ]

        # Pre-populate L2 (simulating previous reads)
        for hash_val, content in files:
            l2_cache.put(hash_val, content)

        times = []
        l1_hits = 0
        l2_hits = 0
        network_reads = 0

        for hash_val, content in files:
            start = time.perf_counter()

            # Check L1 cache
            if hash_val in l1_cache:
                result = l1_cache[hash_val]
                l1_hits += 1
            else:
                # L1 miss → Check L2
                result = l2_cache.get(hash_val)
                if result is not None:
                    l2_hits += 1
                else:
                    # L2 miss → Network read
                    time.sleep(simulate_network_latency() / 1000)
                    result = content
                    network_reads += 1
                    l2_cache.put(hash_val, result)

                # Populate L1 (with eviction)
                if len(l1_cache) >= l1_max_size:
                    l1_cache.pop(next(iter(l1_cache)))
                l1_cache[hash_val] = result

            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

            # Second access (should hit L1)
            start = time.perf_counter()
            if hash_val in l1_cache:
                result = l1_cache[hash_val]
                l1_hits += 1
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

        print(f"\n  Results ({num_files} files, {file_size} bytes each):")
        print(f"    L1 hits:      {l1_hits}")
        print(f"    L2 hits:      {l2_hits}")
        print(f"    Network reads: {network_reads}")
        print(f"    Avg latency:  {statistics.mean(times):.4f}ms")
        print(f"    P95 latency:  {sorted(times)[int(len(times) * 0.95)]:.4f}ms")

        l2_cache.close()

        return {
            "name": "With L2",
            "l1_hits": l1_hits,
            "l2_hits": l2_hits,
            "network_reads": network_reads,
            "avg_ms": statistics.mean(times),
            "p95_ms": sorted(times)[int(len(times) * 0.95)],
            "total_ms": sum(times),
        }


def benchmark_cold_start(num_files: int = 50, file_size: int = 4096) -> dict:
    """Benchmark cold start scenario (L2 not populated yet).

    First access: Network read → Populate L2
    Second access: L2 hit → Fast SSD read
    """
    from nexus.storage.local_disk_cache import LocalDiskCache

    print("\n" + "=" * 70)
    print("COLD START SCENARIO (First access populates L2)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        l2_cache = LocalDiskCache(cache_dir=tmpdir, max_size_gb=1.0)

        files = [
            (content_hash(os.urandom(file_size)), os.urandom(file_size)) for _ in range(num_files)
        ]

        first_access_times = []
        second_access_times = []

        for hash_val, content in files:
            # First access (cold - network read)
            start = time.perf_counter()
            result = l2_cache.get(hash_val)
            if result is None:
                time.sleep(simulate_network_latency() / 1000)  # Simulate network
                result = content
                l2_cache.put(hash_val, result)
            elapsed = (time.perf_counter() - start) * 1000
            first_access_times.append(elapsed)

            # Second access (warm - L2 hit)
            start = time.perf_counter()
            result = l2_cache.get(hash_val)
            elapsed = (time.perf_counter() - start) * 1000
            second_access_times.append(elapsed)

        print(f"\n  Results ({num_files} files):")
        print(f"    First access (cold):  {statistics.mean(first_access_times):.2f}ms avg")
        print(f"    Second access (warm): {statistics.mean(second_access_times):.4f}ms avg")
        print(
            f"    Speedup:              {statistics.mean(first_access_times) / statistics.mean(second_access_times):.0f}x"
        )

        l2_cache.close()

        return {
            "first_access_avg_ms": statistics.mean(first_access_times),
            "second_access_avg_ms": statistics.mean(second_access_times),
            "speedup": statistics.mean(first_access_times) / statistics.mean(second_access_times),
        }


def run_benchmarks():
    print("=" * 70)
    print("FUSE CACHE HIERARCHY BENCHMARK (Issue #1072)")
    print("=" * 70)
    print("\nThis benchmark simulates the FUSE read path with/without LocalDiskCache")
    print("Network latency is simulated at 20ms (typical for remote backends)")

    # Run benchmarks
    without_l2 = benchmark_without_l2(num_files=100, file_size=4096)
    with_l2 = benchmark_with_l2(num_files=100, file_size=4096)
    cold_start = benchmark_cold_start(num_files=50, file_size=4096)

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    speedup = without_l2["avg_ms"] / with_l2["avg_ms"]
    total_speedup = without_l2["total_ms"] / with_l2["total_ms"]

    print(f"\n  {'Scenario':<25} {'Avg Latency':<15} {'Total Time':<15}")
    print(f"  {'-' * 25} {'-' * 15} {'-' * 15}")
    print(
        f"  {'Without L2 (current)':<25} {without_l2['avg_ms']:.2f}ms        {without_l2['total_ms']:.0f}ms"
    )
    print(f"  {'With L2 (new)':<25} {with_l2['avg_ms']:.4f}ms       {with_l2['total_ms']:.2f}ms")
    print(f"  {'Speedup':<25} {speedup:.0f}x             {total_speedup:.0f}x")

    print("\n  Cold Start Performance:")
    print(f"    First access:  {cold_start['first_access_avg_ms']:.2f}ms (network)")
    print(f"    Second access: {cold_start['second_access_avg_ms']:.4f}ms (L2 cache)")
    print(f"    Speedup:       {cold_start['speedup']:.0f}x")

    print("\n  Expected FUSE Impact:")
    print(
        f"    - grep 100 files: {without_l2['total_ms'] / 1000:.1f}s → {with_l2['total_ms'] / 1000:.2f}s ({total_speedup:.0f}x faster)"
    )
    print(
        f"    - IDE file open:  {without_l2['avg_ms']:.0f}ms → {with_l2['avg_ms']:.2f}ms (near-instant)"
    )
    print("    - Build/compile:  Network-bound → SSD-bound")


if __name__ == "__main__":
    run_benchmarks()
