#!/usr/bin/env python3
"""
Demonstration of Rust-accelerated ReBAC permission checking.

This script shows how to use the fast batch permission checking with
Rust acceleration for significant performance improvements.
"""

import time

from nexus.core.rebac_fast import get_performance_stats, is_rust_available
from nexus.core.rebac_manager import ReBACManager


def setup_demo_data(manager: ReBACManager) -> None:
    """Set up demo permission data."""
    print("Setting up demo data...")

    # Create some users
    users = ["alice", "bob", "charlie", "diana", "eve"]

    # Create some files
    files = [f"file{i}" for i in range(100)]

    # Grant permissions: each user can read some files
    for i, user in enumerate(users):
        # Each user gets access to 20 files
        for j in range(i * 20, (i + 1) * 20):
            file_id = files[j % len(files)]
            manager.rebac_write(
                subject_type="user",
                subject_id=user,
                relation="read",
                object_type="file",
                object_id=file_id,
            )

    print(f"✓ Created permissions for {len(users)} users and {len(files)} files")


def benchmark_batch_checking(manager: ReBACManager, checks: list) -> dict:
    """Benchmark permission checking with and without Rust."""
    results = {}

    # Warm up cache
    manager.rebac_check_batch(checks[:10])

    # Clear cache to get fair comparison
    if hasattr(manager, "_l1_cache") and manager._l1_cache:
        manager._l1_cache._cache.clear()

    # Test 1: Python-only (original method)
    print("\n" + "=" * 60)
    print("Test 1: Python-only batch checking")
    print("=" * 60)
    start = time.perf_counter()
    python_results = manager.rebac_check_batch(checks)
    python_time = time.perf_counter() - start
    results["python"] = {
        "time": python_time,
        "results": python_results,
        "per_check": python_time / len(checks) * 1000000,  # microseconds
    }
    print(f"Time: {python_time * 1000:.2f}ms")
    print(f"Per check: {results['python']['per_check']:.2f}µs")
    print(f"Throughput: {len(checks) / python_time:.0f} checks/sec")

    # Clear cache again
    if hasattr(manager, "_l1_cache") and manager._l1_cache:
        manager._l1_cache._cache.clear()

    # Test 2: Rust-accelerated (if available)
    if is_rust_available():
        print("\n" + "=" * 60)
        print("Test 2: Rust-accelerated batch checking")
        print("=" * 60)
        start = time.perf_counter()
        rust_results = manager.rebac_check_batch_fast(checks, use_rust=True)
        rust_time = time.perf_counter() - start
        results["rust"] = {
            "time": rust_time,
            "results": rust_results,
            "per_check": rust_time / len(checks) * 1000000,  # microseconds
        }
        print(f"Time: {rust_time * 1000:.2f}ms")
        print(f"Per check: {results['rust']['per_check']:.2f}µs")
        print(f"Throughput: {len(checks) / rust_time:.0f} checks/sec")

        # Calculate speedup
        speedup = python_time / rust_time
        results["speedup"] = speedup

        print(f"\n🚀 Speedup: {speedup:.1f}x faster with Rust!")

        # Verify results match
        if python_results == rust_results:
            print("✓ Results match between Python and Rust implementations")
        else:
            print("⚠ WARNING: Results differ between implementations!")
            # Find differences
            for i, (p, r) in enumerate(zip(python_results, rust_results, strict=False)):
                if p != r:
                    print(f"  Check {i}: Python={p}, Rust={r}")

    else:
        print("\n⚠ Rust acceleration not available")
        print("Install with: cd rust/nexus_fast && maturin develop --release")

    return results


def demo_use_cases(manager: ReBACManager) -> None:
    """Demonstrate practical use cases for batch checking."""
    print("\n" + "=" * 60)
    print("Practical Use Cases")
    print("=" * 60)

    # Use Case 1: List all readable files for a user
    print("\nUse Case 1: List readable files for user 'alice'")
    print("-" * 60)

    all_files = [f"file{i}" for i in range(100)]
    checks = [(("user", "alice"), "read", ("file", f)) for f in all_files]

    start = time.perf_counter()
    if is_rust_available():
        results = manager.rebac_check_batch_fast(checks)
    else:
        results = manager.rebac_check_batch(checks)
    elapsed = time.perf_counter() - start

    readable_files = [all_files[i] for i, allowed in enumerate(results) if allowed]

    print(f"Checked {len(checks)} files in {elapsed * 1000:.2f}ms")
    print(f"User 'alice' can read {len(readable_files)} files")
    print(f"Sample: {readable_files[:5]}")

    # Use Case 2: Check multiple permissions for multiple users
    print("\nUse Case 2: Check permissions for all users")
    print("-" * 60)

    users = ["alice", "bob", "charlie", "diana", "eve"]
    sample_files = [f"file{i}" for i in range(20)]
    checks = [(("user", user), "read", ("file", f)) for user in users for f in sample_files]

    start = time.perf_counter()
    if is_rust_available():
        results = manager.rebac_check_batch_fast(checks)
    else:
        results = manager.rebac_check_batch(checks)
    elapsed = time.perf_counter() - start

    print(f"Checked {len(checks)} permissions in {elapsed * 1000:.2f}ms")
    print(f"Average: {elapsed / len(checks) * 1000000:.2f}µs per check")

    # Aggregate by user
    allowed_count = {}
    for i, ((_, user), _, _) in enumerate(checks):
        if results[i]:
            allowed_count[user] = allowed_count.get(user, 0) + 1

    print("\nPermissions per user:")
    for user, count in allowed_count.items():
        print(f"  {user}: {count}/{len(sample_files)} files")


def main():
    """Run the demonstration."""
    print("=" * 60)
    print("Rust-Accelerated ReBAC Permission Checking Demo")
    print("=" * 60)

    # Check Rust availability
    stats = get_performance_stats()
    print(f"\nRust acceleration: {'✓ Available' if stats['rust_available'] else '✗ Not available'}")
    if stats["rust_available"]:
        print(f"Expected speedup: {stats['expected_speedup']}")
        print(f"Recommended batch size: {stats['recommended_batch_size']}")

    # Note: This demo requires a running Nexus database
    # For a real demo, use an existing Nexus instance
    print("\n⚠ This demo requires database setup.")
    print("Please see the unit test demo instead:")
    print("  python3 rust/nexus_fast/test_nexus_fast.py")
    print("\nOr integrate into your existing Nexus application.")
    # return

    # # Placeholder for actual implementation
    # # engine = None  # Would use actual engine
    # # manager = None  # Would use actual manager

    # # Set up demo data
    # # setup_demo_data(manager)

    # # Benchmark with increasing batch sizes
    # # batch_sizes = [10, 50, 100, 500, 1000]

    # # print("\n" + "=" * 60)
    # # print("Benchmark: Performance vs Batch Size")
    # # print("=" * 60)

    # # for size in batch_sizes:
    # #     if size > 100 and not is_rust_available():
    # #         # Skip large batches if Rust not available (too slow)
    # #         print(f"\nSkipping batch size {size} (Rust not available)")
    # #         continue

    # #     print(f"\n--- Batch size: {size} ---")

    # #     # Generate checks
    # #     checks = [
    # #         (("user", f"user{i % 5}"), "read", ("file", f"file{i % 100}")) for i in range(size)
    # #     ]

    # #     benchmark_batch_checking(manager, checks)

    # # Demonstrate practical use cases
    # # demo_use_cases(manager)

    print("\n" + "=" * 60)
    print("Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
