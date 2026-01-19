#!/usr/bin/env python3
"""Benchmark LocalDiskCache performance (Issue #1072).

Measures:
1. Native SSD read latency (baseline)
2. LocalDiskCache read latency (L2 cache)
3. Cache miss vs hit performance
4. Throughput for sequential reads

Run: python benchmarks/performance/bench_local_disk_cache.py
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

from nexus.storage.local_disk_cache import LocalDiskCache


def content_hash(content: bytes) -> str:
    """Compute SHA-256 hash of content."""
    return hashlib.sha256(content).hexdigest()


def benchmark_native_ssd(tmp_dir: str, num_files: int = 100, file_size: int = 1024) -> dict:
    """Benchmark native Python file reads from SSD."""
    # Create test files
    files = []
    for i in range(num_files):
        path = Path(tmp_dir) / f"file_{i:04d}.bin"
        content = os.urandom(file_size)
        path.write_bytes(content)
        files.append(path)

    # Warm up OS page cache
    for path in files:
        path.read_bytes()

    # Benchmark reads
    times = []
    for path in files:
        start = time.perf_counter()
        _ = path.read_bytes()
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)  # Convert to ms

    return {
        "name": "Native SSD",
        "avg_ms": statistics.mean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "p95_ms": sorted(times)[int(num_files * 0.95)],
        "throughput_files_sec": num_files / (sum(times) / 1000),
        "throughput_mb_sec": (num_files * file_size) / (sum(times) / 1000) / (1024 * 1024),
    }


def benchmark_local_disk_cache_hits(
    cache: LocalDiskCache, num_files: int = 100, file_size: int = 1024
) -> dict:
    """Benchmark LocalDiskCache read hits."""
    # Pre-populate cache
    contents = []
    for _ in range(num_files):
        content = os.urandom(file_size)
        hash_val = content_hash(content)
        contents.append((hash_val, content))
        cache.put(hash_val, content)

    # Warm up
    for hash_val, _ in contents:
        cache.get(hash_val)

    # Benchmark cache hits
    times = []
    for hash_val, expected_content in contents:
        start = time.perf_counter()
        result = cache.get(hash_val)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)
        assert result == expected_content, "Content mismatch!"

    return {
        "name": "LocalDiskCache HIT",
        "avg_ms": statistics.mean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "p95_ms": sorted(times)[int(num_files * 0.95)],
        "throughput_files_sec": num_files / (sum(times) / 1000),
        "throughput_mb_sec": (num_files * file_size) / (sum(times) / 1000) / (1024 * 1024),
    }


def benchmark_local_disk_cache_misses(cache: LocalDiskCache, num_files: int = 100) -> dict:
    """Benchmark LocalDiskCache read misses (Bloom filter optimization)."""
    # Generate random hashes that don't exist in cache
    hashes = [content_hash(f"nonexistent_{i}".encode()) for i in range(num_files)]

    # Benchmark cache misses
    times = []
    for hash_val in hashes:
        start = time.perf_counter()
        result = cache.get(hash_val)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)
        assert result is None, "Expected cache miss!"

    return {
        "name": "LocalDiskCache MISS (Bloom)",
        "avg_ms": statistics.mean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "p95_ms": sorted(times)[int(num_files * 0.95)],
        "throughput_files_sec": num_files / (sum(times) / 1000),
    }


def benchmark_cache_write(
    cache: LocalDiskCache, num_files: int = 100, file_size: int = 1024
) -> dict:
    """Benchmark LocalDiskCache write performance."""
    contents = [
        (content_hash(os.urandom(file_size)), os.urandom(file_size)) for _ in range(num_files)
    ]

    times = []
    for hash_val, content in contents:
        start = time.perf_counter()
        cache.put(hash_val, content)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)

    return {
        "name": "LocalDiskCache WRITE",
        "avg_ms": statistics.mean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "p95_ms": sorted(times)[int(num_files * 0.95)],
        "throughput_files_sec": num_files / (sum(times) / 1000),
        "throughput_mb_sec": (num_files * file_size) / (sum(times) / 1000) / (1024 * 1024),
    }


def benchmark_large_files(
    cache: LocalDiskCache, file_sizes_kb: list[int] | None = None
) -> list[dict]:
    """Benchmark with different file sizes."""
    if file_sizes_kb is None:
        file_sizes_kb = [1, 10, 100, 1000]
    results = []
    for size_kb in file_sizes_kb:
        file_size = size_kb * 1024
        content = os.urandom(file_size)
        hash_val = content_hash(content)

        # Write
        cache.put(hash_val, content)

        # Read multiple times
        times = []
        for _ in range(20):
            start = time.perf_counter()
            result = cache.get(hash_val)
            elapsed = time.perf_counter() - start
            times.append(elapsed * 1000)
            assert result == content

        results.append(
            {
                "size_kb": size_kb,
                "avg_ms": statistics.mean(times),
                "throughput_mb_sec": file_size / (statistics.mean(times) / 1000) / (1024 * 1024),
            }
        )

        # Clean up
        cache.remove(hash_val)

    return results


def run_benchmarks():
    """Run all benchmarks."""
    print("=" * 70)
    print("LOCAL DISK CACHE BENCHMARK (Issue #1072)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_dir = Path(tmp_dir) / "cache"
        native_dir = Path(tmp_dir) / "native"
        native_dir.mkdir()

        cache = LocalDiskCache(
            cache_dir=str(cache_dir),
            max_size_gb=1.0,  # 1GB for testing
        )

        # 1. Native SSD baseline
        print("\n[1] NATIVE SSD READS (baseline)")
        print("-" * 50)
        native_results = benchmark_native_ssd(str(native_dir), num_files=100, file_size=4096)
        print(f"  Avg latency:    {native_results['avg_ms']:.3f}ms")
        print(f"  P95 latency:    {native_results['p95_ms']:.3f}ms")
        print(f"  Throughput:     {native_results['throughput_files_sec']:.0f} files/sec")
        print(f"  Throughput:     {native_results['throughput_mb_sec']:.1f} MB/sec")

        # 2. LocalDiskCache hits
        print("\n[2] LOCAL DISK CACHE HITS")
        print("-" * 50)
        hit_results = benchmark_local_disk_cache_hits(cache, num_files=100, file_size=4096)
        print(f"  Avg latency:    {hit_results['avg_ms']:.3f}ms")
        print(f"  P95 latency:    {hit_results['p95_ms']:.3f}ms")
        print(f"  Throughput:     {hit_results['throughput_files_sec']:.0f} files/sec")
        print(f"  Throughput:     {hit_results['throughput_mb_sec']:.1f} MB/sec")

        # 3. LocalDiskCache misses (Bloom filter)
        print("\n[3] LOCAL DISK CACHE MISSES (Bloom filter)")
        print("-" * 50)
        miss_results = benchmark_local_disk_cache_misses(cache, num_files=100)
        print(f"  Avg latency:    {miss_results['avg_ms']:.4f}ms")
        print(f"  P95 latency:    {miss_results['p95_ms']:.4f}ms")
        print(f"  Throughput:     {miss_results['throughput_files_sec']:.0f} lookups/sec")

        # 4. Write performance
        print("\n[4] LOCAL DISK CACHE WRITES")
        print("-" * 50)
        cache.clear()  # Clear for fresh write test
        write_results = benchmark_cache_write(cache, num_files=100, file_size=4096)
        print(f"  Avg latency:    {write_results['avg_ms']:.3f}ms")
        print(f"  P95 latency:    {write_results['p95_ms']:.3f}ms")
        print(f"  Throughput:     {write_results['throughput_files_sec']:.0f} files/sec")
        print(f"  Throughput:     {write_results['throughput_mb_sec']:.1f} MB/sec")

        # 5. Large file performance
        print("\n[5] LARGE FILE PERFORMANCE")
        print("-" * 50)
        cache.clear()
        large_results = benchmark_large_files(cache, file_sizes_kb=[1, 10, 100, 1000])
        print(f"  {'Size':<10} {'Latency':<12} {'Throughput':<15}")
        print(f"  {'-' * 10} {'-' * 12} {'-' * 15}")
        for r in large_results:
            print(
                f"  {r['size_kb']:>6} KB  {r['avg_ms']:>8.3f}ms  {r['throughput_mb_sec']:>10.1f} MB/sec"
            )

        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"\n  {'Operation':<30} {'Latency':<12} {'vs Native'}")
        print(f"  {'-' * 30} {'-' * 12} {'-' * 10}")
        print(f"  {'Native SSD read':<30} {native_results['avg_ms']:.3f}ms      1.0x")
        print(
            f"  {'LocalDiskCache HIT':<30} {hit_results['avg_ms']:.3f}ms      {native_results['avg_ms'] / hit_results['avg_ms']:.1f}x"
        )
        print(
            f"  {'LocalDiskCache MISS':<30} {miss_results['avg_ms']:.4f}ms     {native_results['avg_ms'] / miss_results['avg_ms']:.0f}x faster"
        )
        print(f"  {'LocalDiskCache WRITE':<30} {write_results['avg_ms']:.3f}ms      -")

        # Cache stats
        print("\n  Cache Stats:")
        stats = cache.get_stats()
        print(f"    Entries: {stats['entries']}")
        print(f"    Size: {stats['size_mb']:.1f} MB")
        print(f"    Hit rate: {stats['hit_rate']:.1%}")

        cache.close()

        # Expected gains vs network (simulated)
        print("\n" + "=" * 70)
        print("EXPECTED GAINS vs NETWORK BACKEND")
        print("=" * 70)
        network_latency_ms = 25.0  # Typical network read latency
        print(f"\n  Assuming network backend latency: {network_latency_ms}ms")
        print(f"\n  {'Operation':<30} {'Before':<12} {'After':<12} {'Speedup'}")
        print(f"  {'-' * 30} {'-' * 12} {'-' * 12} {'-' * 10}")
        print(
            f"  {'Read (4KB file)':<30} {network_latency_ms:.1f}ms       {hit_results['avg_ms']:.3f}ms      {network_latency_ms / hit_results['avg_ms']:.0f}x"
        )
        print(
            f"  {'Read (1MB file)':<30} {network_latency_ms * 4:.1f}ms      {large_results[3]['avg_ms']:.3f}ms      {(network_latency_ms * 4) / large_results[3]['avg_ms']:.0f}x"
        )
        print(
            f"  {'Cache miss check':<30} {network_latency_ms:.1f}ms       {miss_results['avg_ms']:.4f}ms     {network_latency_ms / miss_results['avg_ms']:.0f}x"
        )


if __name__ == "__main__":
    run_benchmarks()
