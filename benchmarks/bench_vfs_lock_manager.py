#!/usr/bin/env python3
"""Benchmark: VFS Lock Manager — Rust vs Python (Issue #1398).

Usage:
    python benchmarks/bench_vfs_lock_manager.py

Reports:
    - Single-thread uncontended acquire/release
    - Multi-thread contended acquire/release
    - Hierarchical path lock (3-level deep)
"""

from __future__ import annotations

import statistics
import threading
import time

from nexus.core.lock_fast import PythonVFSLockManager

# Try to load Rust implementation.
try:
    from nexus.core.lock_fast import RustVFSLockManager

    HAS_RUST = True
except (ImportError, Exception):
    HAS_RUST = False


def _bench_uncontended(cls: type, ops: int = 10_000) -> dict:
    """Single-thread uncontended acquire + release."""
    mgr = cls()
    latencies_ns: list[int] = []

    for i in range(ops):
        path = f"/bench/{i}"
        start = time.perf_counter_ns()
        h = mgr.acquire(path, "write")
        mgr.release(h)
        latencies_ns.append(time.perf_counter_ns() - start)

    latencies_ns.sort()
    return {
        "ops": ops,
        "avg_ns": statistics.mean(latencies_ns),
        "p50_ns": latencies_ns[len(latencies_ns) // 2],
        "p99_ns": latencies_ns[int(len(latencies_ns) * 0.99)],
        "ops_per_sec": int(ops / (sum(latencies_ns) / 1e9)),
    }


def _bench_contended_reads(cls: type, threads: int = 10, ops_per_thread: int = 1_000) -> dict:
    """Multi-thread contended read acquire + release on same path."""
    mgr = cls()
    total_ops = threads * ops_per_thread
    barrier = threading.Barrier(threads)
    latencies_ns: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        local_lat: list[int] = []
        barrier.wait()
        for _ in range(ops_per_thread):
            start = time.perf_counter_ns()
            h = mgr.acquire("/contended", "read", timeout_ms=1000)
            if h > 0:
                mgr.release(h)
            local_lat.append(time.perf_counter_ns() - start)
        with lock:
            latencies_ns.extend(local_lat)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    wall_start = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    wall_elapsed = time.perf_counter() - wall_start

    latencies_ns.sort()
    return {
        "ops": total_ops,
        "threads": threads,
        "wall_sec": round(wall_elapsed, 3),
        "avg_ns": int(statistics.mean(latencies_ns)),
        "p50_ns": latencies_ns[len(latencies_ns) // 2],
        "p99_ns": latencies_ns[int(len(latencies_ns) * 0.99)],
        "ops_per_sec": int(total_ops / wall_elapsed),
    }


def _bench_hierarchical(cls: type, ops: int = 5_000) -> dict:
    """Hierarchical path lock — acquire deep paths, release in order."""
    mgr = cls()
    latencies_ns: list[int] = []

    for i in range(ops):
        path = f"/zone/tenant/agent/{i}/file.txt"
        start = time.perf_counter_ns()
        h = mgr.acquire(path, "write")
        mgr.release(h)
        latencies_ns.append(time.perf_counter_ns() - start)

    latencies_ns.sort()
    return {
        "ops": ops,
        "avg_ns": int(statistics.mean(latencies_ns)),
        "p50_ns": latencies_ns[len(latencies_ns) // 2],
        "p99_ns": latencies_ns[int(len(latencies_ns) * 0.99)],
        "ops_per_sec": int(ops / (sum(latencies_ns) / 1e9)),
    }


def _print_result(name: str, result: dict) -> None:
    print(f"  {name}:")
    for k, v in result.items():
        if k.endswith("_ns"):
            print(f"    {k}: {v:,.0f} ns")
        elif k == "ops_per_sec":
            print(f"    {k}: {v:,}")
        else:
            print(f"    {k}: {v}")


def main() -> None:
    print("=" * 60)
    print("VFS Lock Manager Benchmark (Issue #1398)")
    print("=" * 60)

    implementations: list[tuple[str, type]] = [("Python", PythonVFSLockManager)]
    if HAS_RUST:
        implementations.append(("Rust", RustVFSLockManager))
    else:
        print("\n  [WARN] Rust implementation not available — skipping.\n")

    for label, cls in implementations:
        print(f"\n--- {label} ({cls.__name__}) ---")
        _print_result("Uncontended (10K ops)", _bench_uncontended(cls))
        _print_result("Contended reads (10 threads x 1K)", _bench_contended_reads(cls))
        _print_result("Hierarchical paths (5K ops)", _bench_hierarchical(cls))

    # Speedup comparison.
    if HAS_RUST:
        print("\n--- Speedup (Rust vs Python) ---")
        py_unc = _bench_uncontended(PythonVFSLockManager, 5_000)
        rs_unc = _bench_uncontended(RustVFSLockManager, 5_000)
        speedup = py_unc["avg_ns"] / rs_unc["avg_ns"] if rs_unc["avg_ns"] > 0 else float("inf")
        print(f"  Uncontended avg: Python={py_unc['avg_ns']:.0f}ns, Rust={rs_unc['avg_ns']:.0f}ns, Speedup={speedup:.1f}x")

    print()


if __name__ == "__main__":
    main()
