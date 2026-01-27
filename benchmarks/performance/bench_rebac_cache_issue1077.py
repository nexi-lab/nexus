#!/usr/bin/env python3
"""Benchmark for Issue #1077: Permission Cache TTL & Invalidation Optimization.

This benchmark compares:
1. Targeted invalidation (O(affected)) vs tenant-wide invalidation (O(n))
2. Tiered TTL performance impact
3. Secondary index overhead

Usage:
    python benchmarks/performance/bench_rebac_cache_issue1077.py
"""

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from nexus.core.rebac_cache import ReBACPermissionCache


def benchmark_invalidation_modes():
    """Compare targeted vs tenant-wide invalidation performance."""
    print("\n" + "=" * 70)
    print("Benchmark: Invalidation Mode Comparison (Issue #1077)")
    print("=" * 70)

    # Test parameters
    num_entries = 10000
    num_subjects = 100
    num_objects_per_subject = num_entries // num_subjects

    results = {}

    for mode in ["targeted", "tenant_wide"]:
        print(f"\n--- Mode: {mode} ---")

        cache = ReBACPermissionCache(
            max_size=50000,
            ttl_seconds=300,
            invalidation_mode=mode,
            enable_metrics=True,
        )

        # Populate cache with entries
        print(f"  Populating {num_entries} entries...")
        start = time.perf_counter()
        for i in range(num_subjects):
            subject_id = f"user_{i}"
            for j in range(num_objects_per_subject):
                object_id = f"/workspace/project_{i}/file_{j}.txt"
                cache.set("agent", subject_id, "read", "file", object_id, True)
        populate_time = time.perf_counter() - start
        print(
            f"  Populate time: {populate_time:.3f}s ({num_entries / populate_time:.0f} entries/s)"
        )

        # Benchmark single subject invalidation
        print("  Benchmarking single subject invalidation...")
        times = []
        for i in range(10):
            subject_id = f"user_{i}"
            start = time.perf_counter()
            cache.invalidate_subject("agent", subject_id)
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        avg_time = sum(times) / len(times)
        print(
            f"  Avg invalidation time: {avg_time * 1000:.3f}ms (invalidated ~{num_objects_per_subject} entries)"
        )

        # Benchmark prefix invalidation
        print("  Benchmarking prefix invalidation...")
        # Re-populate for prefix test
        for i in range(10, 20):
            subject_id = f"user_{i}"
            for j in range(num_objects_per_subject):
                object_id = f"/workspace/project_{i}/file_{j}.txt"
                cache.set("agent", subject_id, "read", "file", object_id, True)

        times = []
        for i in range(10, 20):
            start = time.perf_counter()
            cache.invalidate_object_prefix("file", f"/workspace/project_{i}")
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        avg_prefix_time = sum(times) / len(times)
        print(f"  Avg prefix invalidation time: {avg_prefix_time * 1000:.3f}ms")

        stats = cache.get_stats()
        results[mode] = {
            "populate_time": populate_time,
            "subject_invalidation_ms": avg_time * 1000,
            "prefix_invalidation_ms": avg_prefix_time * 1000,
            "subject_index_size": stats.get("subject_index_size", 0),
            "object_index_size": stats.get("object_index_size", 0),
            "path_prefix_index_size": stats.get("path_prefix_index_size", 0),
        }

    # Compare results
    print("\n--- Results Comparison ---")
    print(f"{'Metric':<35} {'Targeted':<15} {'Tenant-wide':<15} {'Speedup':<10}")
    print("-" * 75)

    targeted = results["targeted"]
    tenant_wide = results["tenant_wide"]

    speedup_subject = tenant_wide["subject_invalidation_ms"] / max(
        targeted["subject_invalidation_ms"], 0.001
    )
    speedup_prefix = tenant_wide["prefix_invalidation_ms"] / max(
        targeted["prefix_invalidation_ms"], 0.001
    )

    print(
        f"{'Subject invalidation (ms)':<35} {targeted['subject_invalidation_ms']:<15.3f} {tenant_wide['subject_invalidation_ms']:<15.3f} {speedup_subject:<10.1f}x"
    )
    print(
        f"{'Prefix invalidation (ms)':<35} {targeted['prefix_invalidation_ms']:<15.3f} {tenant_wide['prefix_invalidation_ms']:<15.3f} {speedup_prefix:<10.1f}x"
    )
    print(f"{'Subject index size':<35} {targeted['subject_index_size']:<15} {'N/A':<15}")
    print(f"{'Object index size':<35} {targeted['object_index_size']:<15} {'N/A':<15}")
    print(f"{'Path prefix index size':<35} {targeted['path_prefix_index_size']:<15} {'N/A':<15}")

    return results


def benchmark_tiered_ttl():
    """Benchmark tiered TTL configuration."""
    print("\n" + "=" * 70)
    print("Benchmark: Tiered TTL (Issue #1077)")
    print("=" * 70)

    cache = ReBACPermissionCache(
        max_size=50000,
        ttl_seconds=300,
        enable_metrics=True,
    )

    # Test different relation types
    relations = [
        ("owner", True),
        ("editor", True),
        ("viewer", True),
        ("read", True),
        ("write", True),
        (None, True),  # No relation - uses default TTL
        ("denial", False),
    ]

    print(f"\n{'Relation':<20} {'Result':<10} {'Expected TTL':<15} {'Actual TTL':<15}")
    print("-" * 60)

    for relation, result in relations:
        cache.set(
            "agent", "alice", "read", "file", f"/test_{relation}.txt", result, relation=relation
        )
        key = cache._make_key("agent", "alice", "read", "file", f"/test_{relation}.txt", None)
        _, actual_ttl, _ = cache._entry_metadata[key]

        if relation is None:
            expected = 300  # Default TTL
            rel_name = "(none)"
        elif not result:
            expected = 60  # Denial TTL
            rel_name = "denial"
        else:
            expected = cache._get_ttl_for_relation(relation)
            rel_name = relation

        # TTL has jitter, so check if within range
        in_range = expected * 0.8 <= actual_ttl <= expected * 1.2
        status = "OK" if in_range else "MISMATCH"

        print(f"{rel_name:<20} {str(result):<10} {expected:<15} {actual_ttl:<15.0f} {status}")


def benchmark_index_overhead():
    """Benchmark the overhead of maintaining secondary indexes."""
    print("\n" + "=" * 70)
    print("Benchmark: Index Overhead (Issue #1077)")
    print("=" * 70)

    num_entries = 50000

    results = {}
    for mode in ["targeted", "tenant_wide"]:
        cache = ReBACPermissionCache(
            max_size=100000,
            ttl_seconds=300,
            invalidation_mode=mode,
        )

        # Measure set() performance
        start = time.perf_counter()
        for i in range(num_entries):
            cache.set("agent", f"user_{i % 100}", "read", "file", f"/path/to/file_{i}.txt", True)
        elapsed = time.perf_counter() - start

        results[mode] = {
            "total_time": elapsed,
            "per_entry_us": (elapsed / num_entries) * 1_000_000,
        }
        print(
            f"\n{mode}: {num_entries} entries in {elapsed:.3f}s ({results[mode]['per_entry_us']:.2f}us/entry)"
        )

    overhead = (
        results["targeted"]["per_entry_us"] / results["tenant_wide"]["per_entry_us"] - 1
    ) * 100
    print(f"\nIndex overhead: {overhead:.1f}%")


def benchmark_deep_hierarchy():
    """Benchmark invalidation with deep directory hierarchies."""
    print("\n" + "=" * 70)
    print("Benchmark: Deep Hierarchy Invalidation (Issue #1077)")
    print("=" * 70)

    cache = ReBACPermissionCache(
        max_size=100000,
        ttl_seconds=300,
        invalidation_mode="targeted",
    )

    # Create entries with varying depths
    depths = [2, 5, 10, 20]
    num_files_per_depth = 1000

    for depth in depths:
        # Clear cache
        cache.clear()

        # Create deep paths
        for i in range(num_files_per_depth):
            path_parts = [f"dir_{j}" for j in range(depth)]
            path = "/" + "/".join(path_parts) + f"/file_{i}.txt"
            cache.set("agent", "alice", "read", "file", path, True)

        # Measure invalidation at different levels
        top_level_path = "/dir_0"
        start = time.perf_counter()
        count = cache.invalidate_object_prefix("file", top_level_path)
        elapsed = time.perf_counter() - start

        print(f"  Depth {depth}: invalidated {count} entries in {elapsed * 1000:.3f}ms")


if __name__ == "__main__":
    print("=" * 70)
    print("Issue #1077: Permission Cache TTL & Invalidation Optimization")
    print("=" * 70)

    benchmark_invalidation_modes()
    benchmark_tiered_ttl()
    benchmark_index_overhead()
    benchmark_deep_hierarchy()

    print("\n" + "=" * 70)
    print("Benchmark Complete")
    print("=" * 70)
