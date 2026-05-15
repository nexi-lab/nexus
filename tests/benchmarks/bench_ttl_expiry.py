"""Benchmark: TTL volume expiry — 100K expired entries < 100ms.

Measures the time to expire 100K entries via expire_ttl_volumes(),
proving the Issue #3405 acceptance criterion:
    - 100K expired files cleaned in < 100ms (vs per-file delete)

Usage:
    python tests/benchmarks/bench_ttl_expiry.py [--count 100000]
"""

from __future__ import annotations

import argparse
import tempfile
import time

import pytest


def make_hash(seed: int) -> str:
    return f"{seed:064x}"


def run_benchmark(count: int = 100_000) -> None:
    from nexus_runtime import BlobPackEngine

    with tempfile.TemporaryDirectory() as d:
        vol_dir = f"{d}/volumes"

        print(f"\n{'=' * 60}")
        print(f"TTL Volume Expiry Benchmark ({count:,} entries)")
        print(f"{'=' * 60}\n")

        # ── Phase 1: Populate with expired entries ────────────────────
        engine = BlobPackEngine(vol_dir, target_volume_size=64 * 1024 * 1024)

        past_expiry = time.time() - 1.0  # already expired
        t0 = time.perf_counter()
        for i in range(count):
            engine.put_with_expiry(make_hash(i), f"data_{i:08d}".encode(), past_expiry)
        populate_time = time.perf_counter() - t0

        engine.seal_active()

        stats = engine.stats()
        print("--- POPULATE ---")
        print(f"  Entries:                {count:>12,}")
        print(f"  Populate time:          {populate_time:>12.3f}s")
        print(f"  Sealed volumes:         {stats['sealed_volume_count']:>12,}")
        print()

        # ── Phase 2: Expire all entries ──────────────────────────────
        t0 = time.perf_counter()
        results = engine.expire_ttl_volumes()
        expire_time = time.perf_counter() - t0

        total_expired = sum(c for _, c in results)
        volumes_cleaned = len(results)

        print("--- EXPIRY ---")
        print(f"  Entries expired:        {total_expired:>12,}")
        print(f"  Volumes cleaned:        {volumes_cleaned:>12,}")
        print(f"  Expire time:            {expire_time * 1000:>12.1f}ms")
        print(
            f"  Per-entry:              {(expire_time / max(total_expired, 1)) * 1_000_000:>12.1f}μs"
        )
        print()

        # ── Phase 3: Verify all entries gone ─────────────────────────
        t0 = time.perf_counter()
        sample_missing = sum(
            1 for i in range(min(1000, count)) if engine.read_content(make_hash(i)) is None
        )
        verify_time = time.perf_counter() - t0

        print("--- VERIFY ---")
        print(f"  Sample checked:         {min(1000, count):>12,}")
        print(f"  Missing (expected):     {sample_missing:>12,}")
        print(f"  Verify time:            {verify_time * 1000:>12.1f}ms")
        print()

        # ── Summary ──────────────────────────────────────────────────
        print(f"{'=' * 60}")
        target_ms = 100
        passed = expire_time * 1000 < target_ms
        status = "✓" if passed else "✗"
        print(
            f"  {status} {count:,} entries expired in {expire_time * 1000:.1f}ms (target: < {target_ms}ms)"
        )
        all_gone = sample_missing == min(1000, count)
        print(f"  {status} All entries verified as gone: {all_gone}")
        print(f"{'=' * 60}")

        engine.close()

        return expire_time * 1000, all_gone


@pytest.mark.timeout(120)
def test_ttl_expiry_benchmark():
    """Acceptance criterion: 100K expired files cleaned in < 100ms."""
    expire_ms, all_gone = run_benchmark(count=100_000)
    assert expire_ms < 100, f"100K expiry took {expire_ms:.1f}ms, target is < 100ms"
    assert all_gone, "Not all expired entries were cleaned"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TTL volume expiry benchmark")
    parser.add_argument("--count", type=int, default=100_000)
    args = parser.parse_args()
    run_benchmark(count=args.count)
