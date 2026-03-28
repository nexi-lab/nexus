#!/usr/bin/env python3
"""Benchmark write performance with and without permission operations.

This script measures:
1. Single file write latency (with permissions)
2. Batch write latency (with permissions)
3. Comparison with native Python and bash

Run: python scripts/benchmark_write_performance.py
"""

import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore
from tests.helpers.test_context import TEST_CONTEXT

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def benchmark_native_python(tmp_dir: str, num_files: int = 50) -> dict:
    """Benchmark native Python file writes."""
    content = b"x" * 1024  # 1KB
    times = []

    native_dir = Path(tmp_dir) / "native_python"
    native_dir.mkdir(parents=True, exist_ok=True)

    for i in range(num_files):
        path = native_dir / f"file_{i:04d}.txt"
        start = time.perf_counter()
        with open(path, "wb") as f:
            f.write(content)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)

    return {
        "avg_ms": statistics.mean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "throughput": num_files / (sum(times) / 1000),
    }


def benchmark_native_bash(tmp_dir: str, num_files: int = 50) -> dict:
    """Benchmark bash file writes using echo."""
    times = []

    bash_dir = Path(tmp_dir) / "native_bash"
    bash_dir.mkdir(parents=True, exist_ok=True)

    content = "x" * 1024  # 1KB string

    for i in range(num_files):
        path = bash_dir / f"file_{i:04d}.txt"
        start = time.perf_counter()
        subprocess.run(
            f'echo -n "{content}" > "{path}"',
            shell=True,
            check=True,
            capture_output=True,
        )
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)

    return {
        "avg_ms": statistics.mean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "throughput": num_files / (sum(times) / 1000),
    }


async def run_benchmark(enable_deferred: bool = False):
    """Run write performance benchmark.

    Args:
        enable_deferred: If True, use deferred permission buffer for faster writes
    """
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.contracts.types import OperationContext
    from nexus.core.config import ParseConfig, PermissionConfig
    from nexus.factory import create_nexus_fs

    mode = "DEFERRED" if enable_deferred else "SYNC"
    print("=" * 70)
    print(f"WRITE PERFORMANCE BENCHMARK ({mode} MODE)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_path = Path(tmp_dir) / "storage"
        storage_path.mkdir()
        db_path = Path(tmp_dir) / "nexus.db"

        backend = CASLocalBackend(str(storage_path))

        # Create NexusFS with permissions ENABLED via factory
        nx = await create_nexus_fs(
            backend=backend,
            metadata_store=RaftMetadataStore.embedded(str(db_path).replace(".db", "-raft")),
            record_store=SQLAlchemyRecordStore(db_path=str(db_path)),
            permissions=PermissionConfig(enforce=True),
            parsing=ParseConfig(auto_parse=False),
            init_cred=TEST_CONTEXT,
        )

        # Create user context
        ctx = OperationContext(
            user_id="benchmark_user",
            groups=[],
            zone_id="benchmark_zone",
            is_admin=False,
        )

        # Grant write permission to root
        if hasattr(nx, "_rebac_manager") and nx._rebac_manager:
            nx._rebac_manager.rebac_write(
                subject=("user", "benchmark_user"),
                relation="direct_editor",
                object=("file", "/"),
                zone_id="benchmark_zone",
            )

        content_1kb = b"x" * 1024
        content_10kb = b"y" * (10 * 1024)

        # =========================================
        # Benchmark 1: Single file writes (1KB)
        # =========================================
        print("\n[1] SINGLE FILE WRITES (1KB content)")
        print("-" * 50)

        single_times = []
        num_files = 50

        for i in range(num_files):
            path = f"/bench/single/file_{i:04d}.txt"
            start = time.perf_counter()
            await nx.write(path, content_1kb, context=ctx)
            elapsed = time.perf_counter() - start
            single_times.append(elapsed * 1000)  # Convert to ms

        print(f"  Files written: {num_files}")
        print(f"  Avg latency:   {statistics.mean(single_times):.2f}ms")
        print(f"  Min latency:   {min(single_times):.2f}ms")
        print(f"  Max latency:   {max(single_times):.2f}ms")
        print(f"  Std dev:       {statistics.stdev(single_times):.2f}ms")
        print(f"  P95 latency:   {sorted(single_times)[int(num_files * 0.95)]:.2f}ms")
        print(f"  Throughput:    {num_files / (sum(single_times) / 1000):.1f} files/sec")

        # =========================================
        # Benchmark 2: Batch writes (1KB each)
        # =========================================
        print("\n[2] BATCH WRITES (100 files x 1KB)")
        print("-" * 50)

        batch_times = []
        num_batches = 5
        batch_size = 100

        for batch_num in range(num_batches):
            files = [
                (f"/bench/batch{batch_num}/file_{i:04d}.txt", content_1kb)
                for i in range(batch_size)
            ]
            start = time.perf_counter()
            nx.write_batch(files, context=ctx)
            elapsed = time.perf_counter() - start
            batch_times.append(elapsed * 1000)

        print(f"  Batches:       {num_batches} x {batch_size} files")
        print(f"  Avg batch:     {statistics.mean(batch_times):.2f}ms")
        print(f"  Per-file avg:  {statistics.mean(batch_times) / batch_size:.2f}ms")
        print(f"  Min batch:     {min(batch_times):.2f}ms")
        print(f"  Max batch:     {max(batch_times):.2f}ms")
        total_files = num_batches * batch_size
        total_time_sec = sum(batch_times) / 1000
        print(f"  Throughput:    {total_files / total_time_sec:.1f} files/sec")

        # =========================================
        # Benchmark 3: Single file writes (10KB)
        # =========================================
        print("\n[3] SINGLE FILE WRITES (10KB content)")
        print("-" * 50)

        single_times_10k = []

        for i in range(num_files):
            path = f"/bench/single10k/file_{i:04d}.txt"
            start = time.perf_counter()
            await nx.write(path, content_10kb, context=ctx)
            elapsed = time.perf_counter() - start
            single_times_10k.append(elapsed * 1000)

        print(f"  Files written: {num_files}")
        print(f"  Avg latency:   {statistics.mean(single_times_10k):.2f}ms")
        print(f"  Min latency:   {min(single_times_10k):.2f}ms")
        print(f"  Max latency:   {max(single_times_10k):.2f}ms")
        print(f"  Throughput:    {num_files / (sum(single_times_10k) / 1000):.1f} files/sec")

        # =========================================
        # Summary
        # =========================================
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"  Single write (1KB):    {statistics.mean(single_times):.2f}ms avg")
        print(f"  Single write (10KB):   {statistics.mean(single_times_10k):.2f}ms avg")
        print(f"  Batch write (per file): {statistics.mean(batch_times) / batch_size:.2f}ms avg")
        print(
            f"  Batch speedup:         {statistics.mean(single_times) / (statistics.mean(batch_times) / batch_size):.1f}x"
        )

        nx.close()

        # Return results for comparison
        return {
            "single_1kb_avg_ms": statistics.mean(single_times),
            "single_10kb_avg_ms": statistics.mean(single_times_10k),
            "batch_per_file_ms": statistics.mean(batch_times) / batch_size,
            "single_throughput": num_files / (sum(single_times) / 1000),
            "batch_throughput": total_files / total_time_sec,
        }


async def run_write_back_benchmark():
    """Benchmark write-back mode with timing breakdown (Issue #3393, T4).

    Measures:
    - VFS lock hold time (content write + metadata enqueue)
    - Metadata enqueue time
    - Batch flush latency (single Raft round for N writes)
    - P50/P95/P99 for all of the above
    """
    import os

    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.contracts.types import OperationContext
    from nexus.core.config import ParseConfig, PermissionConfig
    from nexus.factory import create_nexus_fs

    print("=" * 70)
    print("WRITE-BACK BENCHMARK (Issue #3393 — metadata buffer)")
    print("=" * 70)

    # Enable the metadata buffer via env var
    os.environ["NEXUS_METADATA_BUFFER"] = "1"

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage_path = Path(tmp_dir) / "storage"
            storage_path.mkdir()
            db_path = Path(tmp_dir) / "nexus.db"

            backend = CASLocalBackend(str(storage_path))
            nx = await create_nexus_fs(
                backend=backend,
                metadata_store=RaftMetadataStore.embedded(str(db_path).replace(".db", "-raft")),
                record_store=SQLAlchemyRecordStore(db_path=str(db_path)),
                permissions=PermissionConfig(enforce=False),
                parsing=ParseConfig(auto_parse=False),
                init_cred=TEST_CONTEXT,
            )

            ctx = OperationContext(
                user_id="benchmark_user",
                groups=[],
                zone_id="benchmark_zone",
                is_admin=True,
            )

            content_1kb = b"x" * 1024
            num_files = 100

            # ---- Benchmark: write-back single writes ----
            print("\n[1] WRITE-BACK SINGLE WRITES (1KB, consistency='wb')")
            print("-" * 50)

            write_times = []
            for i in range(num_files):
                path = f"/bench/wb/file_{i:04d}.txt"
                start = time.perf_counter()
                await nx.write(path, content_1kb, context=ctx, consistency="wb")
                elapsed = time.perf_counter() - start
                write_times.append(elapsed * 1000)

            write_times_sorted = sorted(write_times)
            p50 = write_times_sorted[int(num_files * 0.50)]
            p95 = write_times_sorted[int(num_files * 0.95)]
            p99 = write_times_sorted[int(num_files * 0.99)]

            print(f"  Files written:   {num_files}")
            print(f"  Avg write time:  {statistics.mean(write_times):.3f}ms")
            print(f"  P50:             {p50:.3f}ms")
            print(f"  P95:             {p95:.3f}ms")
            print(f"  P99:             {p99:.3f}ms")
            print(f"  Min:             {min(write_times):.3f}ms")
            print(f"  Max:             {max(write_times):.3f}ms")
            print(f"  Throughput:      {num_files / (sum(write_times) / 1000):.1f} files/sec")

            # ---- Benchmark: SC single writes for comparison ----
            print("\n[2] SC SINGLE WRITES (1KB, consistency='sc')")
            print("-" * 50)

            sc_times = []
            for i in range(num_files):
                path = f"/bench/sc/file_{i:04d}.txt"
                start = time.perf_counter()
                await nx.write(path, content_1kb, context=ctx, consistency="sc")
                elapsed = time.perf_counter() - start
                sc_times.append(elapsed * 1000)

            sc_sorted = sorted(sc_times)
            print(f"  Files written:   {num_files}")
            print(f"  Avg write time:  {statistics.mean(sc_times):.3f}ms")
            print(f"  P50:             {sc_sorted[int(num_files * 0.50)]:.3f}ms")
            print(f"  P95:             {sc_sorted[int(num_files * 0.95)]:.3f}ms")
            print(f"  P99:             {sc_sorted[int(num_files * 0.99)]:.3f}ms")
            print(f"  Throughput:      {num_files / (sum(sc_times) / 1000):.1f} files/sec")

            # ---- Benchmark: flush latency ----
            print("\n[3] FLUSH LATENCY (batched Raft commit)")
            print("-" * 50)

            from nexus.storage.buffered_metadata_store import BufferedMetadataStore

            if isinstance(nx.metadata, BufferedMetadataStore):
                # Write a fresh batch to measure flush
                flush_batch_sizes = [10, 50, 100]
                for batch_n in flush_batch_sizes:
                    for i in range(batch_n):
                        path = f"/bench/flush_{batch_n}/file_{i:04d}.txt"
                        await nx.write(path, content_1kb, context=ctx, consistency="wb")

                    start = time.perf_counter()
                    nx.metadata.flush()
                    flush_elapsed = (time.perf_counter() - start) * 1000

                    print(
                        f"  Batch size {batch_n:>3d}: "
                        f"{flush_elapsed:.2f}ms total, "
                        f"{flush_elapsed / batch_n:.3f}ms/item"
                    )

                stats = nx.metadata.get_buffer_stats()
                print(f"\n  Buffer stats: {stats}")
            else:
                print("  (metadata buffer not active — skipping flush benchmark)")

            # ---- Summary ----
            print("\n" + "=" * 70)
            print("WRITE-BACK vs SC COMPARISON")
            print("=" * 70)
            wb_avg = statistics.mean(write_times)
            sc_avg = statistics.mean(sc_times)
            if sc_avg > 0:
                speedup = sc_avg / wb_avg
                print(f"  WB avg:    {wb_avg:.3f}ms")
                print(f"  SC avg:    {sc_avg:.3f}ms")
                print(f"  Speedup:   {speedup:.1f}x")

            nx.close()

            return {
                "wb_avg_ms": wb_avg,
                "wb_p50_ms": p50,
                "wb_p95_ms": p95,
                "wb_p99_ms": p99,
                "sc_avg_ms": sc_avg,
                "speedup": speedup if sc_avg > 0 else 0,
            }
    finally:
        os.environ.pop("NEXUS_METADATA_BUFFER", None)


if __name__ == "__main__":
    import json

    # Check command line args
    if len(sys.argv) > 1 and sys.argv[1] == "--write-back":
        # Run write-back mode benchmark (Issue #3393)
        results = run_write_back_benchmark()
        print("\n[JSON Results - WRITE-BACK]")
        print(json.dumps(results, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "--deferred":
        # Run only deferred mode
        results = run_benchmark(enable_deferred=True)
        print("\n[JSON Results - DEFERRED]")
        print(json.dumps(results, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "--compare":
        # Run full comparison including native
        print("\n" + "=" * 70)
        print("FULL COMPARISON: NATIVE vs SYNC vs DEFERRED")
        print("=" * 70)

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Native benchmarks
            print("\n[0] NATIVE PYTHON FILE WRITES (1KB)")
            print("-" * 50)
            native_python = benchmark_native_python(tmp_dir)
            print(f"  Avg latency:   {native_python['avg_ms']:.3f}ms")
            print(f"  Throughput:    {native_python['throughput']:.0f} files/sec")

            print("\n[0] NATIVE BASH FILE WRITES (1KB)")
            print("-" * 50)
            native_bash = benchmark_native_bash(tmp_dir)
            print(f"  Avg latency:   {native_bash['avg_ms']:.3f}ms")
            print(f"  Throughput:    {native_bash['throughput']:.0f} files/sec")

        # Nexus benchmarks
        sync_results = run_benchmark(enable_deferred=False)
        print("\n")
        deferred_results = run_benchmark(enable_deferred=True)

        print("\n" + "=" * 70)
        print("COMPARISON RESULTS")
        print("=" * 70)
        print(f"\n  {'Method':<20} {'Latency':<12} {'Throughput':<15} {'vs Native'}")
        print(f"  {'-' * 20} {'-' * 12} {'-' * 15} {'-' * 10}")
        print(
            f"  {'Native Python':<20} {native_python['avg_ms']:.3f}ms      {native_python['throughput']:.0f} files/sec    1.0x"
        )
        print(
            f"  {'Native Bash':<20} {native_bash['avg_ms']:.3f}ms      {native_bash['throughput']:.0f} files/sec    {native_python['avg_ms'] / native_bash['avg_ms']:.1f}x"
        )
        print(
            f"  {'Nexus (SYNC)':<20} {sync_results['single_1kb_avg_ms']:.2f}ms       {sync_results['single_throughput']:.0f} files/sec     {sync_results['single_1kb_avg_ms'] / native_python['avg_ms']:.0f}x slower"
        )
        print(
            f"  {'Nexus (DEFERRED)':<20} {deferred_results['single_1kb_avg_ms']:.2f}ms       {deferred_results['single_throughput']:.0f} files/sec     {deferred_results['single_1kb_avg_ms'] / native_python['avg_ms']:.0f}x slower"
        )

        print(
            f"\n  Deferred vs Sync speedup: {sync_results['single_1kb_avg_ms'] / deferred_results['single_1kb_avg_ms']:.1f}x"
        )
        print("\n  Note: Nexus adds permissions, metadata, CAS deduplication, audit logging.")
        print("  The overhead is the cost of enterprise features.")
    else:
        # Default: run sync mode
        results = run_benchmark(enable_deferred=False)
        print("\n[JSON Results - SYNC]")
        print(json.dumps(results, indent=2))
        print("\nRun with --deferred for deferred mode, or --compare for comparison")
