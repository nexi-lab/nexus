"""Benchmark: in-memory volume index — O(1) content lookup.

Measures:
  - Startup index load time (target: < 200ms for 1M entries)
  - Per-lookup latency via read_content (target: < 100μs)
  - Memory usage per entry (target: < 40 bytes)

Issue #3404: in-memory volume index.

Usage:
    python tests/benchmarks/bench_mem_index.py [--count 10000]
"""

from __future__ import annotations

import argparse
import os
import statistics
import tempfile
import time

import pytest


def make_hash(seed: int) -> str:
    return f"{seed:064x}"


def run_benchmark(count: int = 10000, read_iterations: int = 500) -> None:
    from nexus_kernel import BlobPackEngine

    with tempfile.TemporaryDirectory() as d:
        vol_dir = os.path.join(d, "volumes")

        print(f"{'=' * 72}")
        print(
            f"In-Memory Volume Index Benchmark — {count:,} entries, {read_iterations} read iterations"
        )
        print(f"{'=' * 72}")
        print()

        # ── Phase 1: Populate ─────────────────────────────────────────────
        engine = BlobPackEngine(vol_dir, target_volume_size=64 * 1024 * 1024)

        t0 = time.perf_counter()
        for i in range(count):
            h = make_hash(i)
            engine.put(h, f"content_{i:08d}".encode())
        populate_time = time.perf_counter() - t0

        engine.seal_active()

        stats = engine.stats()
        mem_bytes = engine.index_memory_bytes()
        per_entry = mem_bytes / count if count > 0 else 0

        print("--- POPULATE ---")
        print(f"  Entries:                {count:>12,}")
        print(f"  Populate time:          {populate_time:>12.3f}s")
        print(f"  mem_index_bytes:        {mem_bytes:>12,}")
        print(f"  Per-entry memory:       {per_entry:>12.1f} bytes")
        print(f"  Sealed volumes:         {stats['sealed_volume_count']:>12,}")
        print(f"  Cached volume FDs:      {stats['mem_index_volumes']:>12,}")
        print()

        # ── Phase 2: Startup load time ────────────────────────────────────
        engine.close()
        del engine

        t0 = time.perf_counter()
        engine = BlobPackEngine(vol_dir, target_volume_size=64 * 1024 * 1024)
        startup_time = time.perf_counter() - t0

        stats2 = engine.stats()
        print("--- STARTUP LOAD ---")
        print(f"  Load time:              {startup_time * 1000:>12.1f}ms")
        print(f"  Entries loaded:         {stats2['mem_index_entries']:>12,}")
        print(f"  Volume FDs opened:      {stats2['mem_index_volumes']:>12,}")
        print()

        # ── Phase 3: Read latency (read_content fast path) ───────────────
        # Warm up
        for i in range(min(100, count)):
            engine.read_content(make_hash(i))

        # Measure read_content latency
        read_times = []
        for _ in range(read_iterations):
            h = make_hash(_ % count)
            t0 = time.perf_counter()
            data = engine.read_content(h)
            elapsed = time.perf_counter() - t0
            read_times.append(elapsed)
            assert data is not None

        read_times_us = [t * 1_000_000 for t in read_times]
        read_times_us.sort()

        print("--- READ LATENCY (read_content) ---")
        print(f"  Median:                 {statistics.median(read_times_us):>12.0f}μs")
        print(f"  P95:                    {read_times_us[int(len(read_times_us) * 0.95)]:>12.0f}μs")
        print(f"  P99:                    {read_times_us[int(len(read_times_us) * 0.99)]:>12.0f}μs")
        print(f"  Avg:                    {statistics.mean(read_times_us):>12.0f}μs")
        print(f"  Min:                    {min(read_times_us):>12.0f}μs")
        print()

        # ── Phase 4: Compare with exists() / get_size() ──────────────────
        exists_times = []
        for _ in range(read_iterations):
            h = make_hash(_ % count)
            t0 = time.perf_counter()
            engine.exists(h)
            elapsed = time.perf_counter() - t0
            exists_times.append(elapsed)

        exists_us = [t * 1_000_000 for t in exists_times]
        exists_us.sort()

        size_times = []
        for _ in range(read_iterations):
            h = make_hash(_ % count)
            t0 = time.perf_counter()
            engine.get_size(h)
            elapsed = time.perf_counter() - t0
            size_times.append(elapsed)

        size_us = [t * 1_000_000 for t in size_times]
        size_us.sort()

        print("--- LOOKUP LATENCY (exists/get_size — no I/O) ---")
        print(f"  exists() median:        {statistics.median(exists_us):>12.0f}μs")
        print(f"  get_size() median:      {statistics.median(size_us):>12.0f}μs")
        print()

        # ── Summary ──────────────────────────────────────────────────────
        print(f"{'=' * 72}")
        read_median = statistics.median(read_times_us)
        checks = []
        checks.append(("Read latency < 100μs", read_median < 100, f"{read_median:.0f}μs"))
        checks.append(("Per-entry memory < 60B", per_entry < 60, f"{per_entry:.1f}B"))
        checks.append(
            (
                f"Startup load < 200ms ({count:,} entries)",
                startup_time * 1000 < 200,
                f"{startup_time * 1000:.1f}ms",
            )
        )

        for label, passed, value in checks:
            status = "✓" if passed else "✗"
            print(f"  {status} {label}: {value}")
        print(f"{'=' * 72}")

        engine.close()


# Pytest entry point — runs at 50K entries to meaningfully extrapolate to 1M.
@pytest.mark.timeout(120)
def test_mem_index_benchmark():
    run_benchmark(count=50000, read_iterations=200)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10000)
    parser.add_argument("--reads", type=int, default=500)
    args = parser.parse_args()
    run_benchmark(count=args.count, read_iterations=args.reads)
