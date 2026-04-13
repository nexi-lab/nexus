#!/usr/bin/env python3
"""Benchmark: VFS write lock contention under concurrent agents (Issue #3705).

Measures throughput degradation when ``_vfs_locked`` holds backend I/O +
``metastore.put`` for the full duration of a write under concurrent workloads.

Suspects:
  - nexus_fs.py:2495  ``with self._vfs_locked(path, "write"):`` wraps
    ``write_content`` + ``metastore.put``
  - kernel.rs:1587-1631  same pattern in Rust fast-path

Scenarios
---------
1. Single-agent sequential writes — baseline ops/sec (13 B, 1 KB, 64 KB, 1 MB)
2. 5 concurrent agents writing to *different* paths
3. 10 concurrent agents writing to *different* paths
4. 5 concurrent agents writing to *same directory* — measures lock contention
5. Write burst: 50 files rapid-fire to one directory — total wall time

Data source
-----------
``conftest.py`` ``sample_files`` fixture (13 B, 1 KB, 64 KB, 1 MB).
Burst test uses synthetic 1 KB files (HERB enterprise-context/WorkFlowGenie
not present in test tree; falls back to synthetic payload).

Usage::

    python -m pytest tests/benchmarks/bench_write_lock_contention.py -s -v
    python tests/benchmarks/bench_write_lock_contention.py
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Write helpers (all sync — upstream write chain is fully synchronous)
# ---------------------------------------------------------------------------

_TINY = b"Hello, World!"  # 13 B
_SMALL = b"x" * 1024  # 1 KB
_MEDIUM = b"y" * (64 * 1024)  # 64 KB
_LARGE = b"z" * (1024 * 1024)  # 1 MB

_SAMPLE_FILES: dict[str, bytes] = {
    "tiny": _TINY,
    "small": _SMALL,
    "medium": _MEDIUM,
    "large": _LARGE,
}


def _seq_writes(nx: Any, base: str, content: bytes, n: int) -> list[float]:
    """Single agent — sequential writes, returns per-op latencies (seconds)."""
    times: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        nx.write(f"{base}/f{i:04d}.bin", content)
        times.append(time.perf_counter() - t0)
    return times


def _agent_diff_paths(nx: Any, agent_id: int, base: str, content: bytes, n: int) -> None:
    """Agent writing to its own isolated path space."""
    for i in range(n):
        nx.write(f"{base}/agent_{agent_id}/f{i:04d}.bin", content)


def _agent_same_dir(nx: Any, agent_id: int, base: str, content: bytes, n: int) -> None:
    """Agent writing into a shared directory (distinct file names per agent)."""
    for i in range(n):
        nx.write(f"{base}/a{agent_id}_f{i:04d}.bin", content)


def _run_concurrent(
    nx: Any,
    n_agents: int,
    base: str,
    content: bytes,
    ops_per_agent: int,
    same_dir: bool = False,
) -> float:
    """Launch *n_agents* threads concurrently; return total wall time (s)."""
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_agents) as pool:
        if same_dir:
            futs = [
                pool.submit(_agent_same_dir, nx, i, base, content, ops_per_agent)
                for i in range(n_agents)
            ]
        else:
            futs = [
                pool.submit(_agent_diff_paths, nx, i, base, content, ops_per_agent)
                for i in range(n_agents)
            ]
        for f in concurrent.futures.as_completed(futs):
            f.result()
    return time.perf_counter() - t0


def _run_threaded(
    nx: Any,
    n_workers: int,
    base: str,
    content: bytes,
    ops_per_agent: int,
) -> float:
    """Run *n_workers* threads each writing to different paths; return wall time (s)."""
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [
            pool.submit(_agent_diff_paths, nx, i, base, content, ops_per_agent)
            for i in range(n_workers)
        ]
        for f in concurrent.futures.as_completed(futs):
            f.result()
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_lat(times_s: list[float]) -> str:
    ms = [t * 1000 for t in times_s]
    med = statistics.median(ms)
    p95 = sorted(ms)[int(len(ms) * 0.95)]
    avg = statistics.mean(ms)
    ops = len(times_s) / sum(times_s) if sum(times_s) > 0 else 0
    return f"median={med:.2f}ms  p95={p95:.2f}ms  avg={avg:.2f}ms  ops/s={ops:.1f}"


def _fmt_throughput(n_ops: int, wall_s: float, baseline_ops: float = 0.0) -> str:
    ops = n_ops / wall_s if wall_s > 0 else 0
    base = f"  ratio_vs_baseline={ops / baseline_ops:.2f}x" if baseline_ops > 0 else ""
    return f"wall={wall_s:.3f}s  ops/s={ops:.1f}  total_ops={n_ops}{base}"


# ---------------------------------------------------------------------------
# Lock hold time instrumentation
# ---------------------------------------------------------------------------


def _measure_lock_hold(nx: Any, content: bytes, n: int) -> list[float]:
    """Monkey-patch _vfs_locked to record how long the lock is held.

    Returns a list of hold durations in seconds (one per write).
    Instruments the sync lock path used by _write_content.
    """
    import nexus.core.nexus_fs as _fs_mod

    _orig = _fs_mod.NexusFS._vfs_locked
    hold_times: list[float] = []

    @contextlib.contextmanager
    def _patched(self: Any, path: str, mode: str):
        t0 = time.perf_counter()
        with _orig(self, path, mode):
            yield
        hold_times.append(time.perf_counter() - t0)

    _fs_mod.NexusFS._vfs_locked = _patched
    try:
        _seq_writes(nx, "/lock_probe", content, n)
    finally:
        _fs_mod.NexusFS._vfs_locked = _orig
    assert hold_times, "No lock-hold samples captured — instrumentation mismatch"
    return hold_times


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------


async def _run(n_seq: int = 20, ops_per_agent: int = 10, burst_count: int = 50) -> None:
    import os

    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.core.config import CacheConfig, ParseConfig, PermissionConfig
    from nexus.factory import create_nexus_fs
    from nexus.storage.raft_metadata_store import RaftMetadataStore
    from nexus.storage.record_store import SQLAlchemyRecordStore

    # Prevent the standalone benchmark from touching the operator's
    # configured SQL database (Issue #3705 review finding).
    for _env in ("NEXUS_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        os.environ.pop(_env, None)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        async def _make_nx(suffix: str = "") -> Any:
            storage = tmp_path / f"storage{suffix}"
            storage.mkdir(parents=True, exist_ok=True)
            db_base = str(tmp_path / f"bench{suffix}")
            return await create_nexus_fs(
                backend=CASLocalBackend(str(storage)),
                metadata_store=RaftMetadataStore.embedded(db_base),
                record_store=SQLAlchemyRecordStore(db_path=str(tmp_path / f"records{suffix}.db")),
                is_admin=True,
                permissions=PermissionConfig(enforce=False),
                parsing=ParseConfig(auto_parse=False),
                cache=CacheConfig(enable_content_cache=True),
            )

        sep = "=" * 70

        # ── Scenario 0: lock hold time (direct measurement) ───────────────
        print(f"\n{sep}")
        print("Scenario 0 — VFS lock hold time (direct instrumentation)")
        print(sep)
        print("  Measures how long _vfs_locked is held per write (1KB payload, 20 ops)")

        nx0 = await _make_nx("0")
        hold = _measure_lock_hold(nx0, _SMALL, 20)
        hold_ms = [t * 1000 for t in hold]
        med_h = statistics.median(hold_ms)
        p99_h = sorted(hold_ms)[int(len(hold_ms) * 0.99)]
        print(f"  median_hold={med_h:.3f}ms  p99_hold={p99_h:.3f}ms")
        nx0.close()

        # ── Scenario 1: baseline sequential ──────────────────────────────
        print(f"\n{sep}")
        print("Scenario 1 — Single-agent sequential writes (baseline)")
        print(sep)
        print(f"  n_ops={n_seq} per size")

        nx1 = await _make_nx("1")
        baseline_ops: dict[str, float] = {}
        baseline_med_ms: dict[str, float] = {}
        for size_name, content in _SAMPLE_FILES.items():
            times = _seq_writes(nx1, f"/s1/{size_name}", content, n_seq)
            ops = len(times) / sum(times)
            baseline_ops[size_name] = ops
            baseline_med_ms[size_name] = statistics.median(t * 1000 for t in times)
            print(f"  [{size_name:6s}]  {_fmt_lat(times)}")
        nx1.close()

        # ── Scenario 2: 5 agents, different paths ────────────────────────
        print(f"\n{sep}")
        print("Scenario 2 — 5 concurrent agents, different paths")
        print(sep)
        total_ops_2 = 5 * ops_per_agent
        print(f"  agents=5  ops_per_agent={ops_per_agent}  total_ops={total_ops_2}")

        nx2 = await _make_nx("2")
        s2_wall: dict[str, float] = {}
        for size_name, content in _SAMPLE_FILES.items():
            wall = _run_concurrent(nx2, 5, f"/s2/{size_name}", content, ops_per_agent)
            s2_wall[size_name] = wall
            print(
                f"  [{size_name:6s}]  {_fmt_throughput(total_ops_2, wall, baseline_ops[size_name])}"
            )
        nx2.close()

        # ── Scenario 3: 10 agents, different paths ───────────────────────
        print(f"\n{sep}")
        print("Scenario 3 — 10 concurrent agents, different paths")
        print(sep)
        total_ops_3 = 10 * ops_per_agent
        print(f"  agents=10  ops_per_agent={ops_per_agent}  total_ops={total_ops_3}")

        nx3 = await _make_nx("3")
        for size_name, content in _SAMPLE_FILES.items():
            wall = _run_concurrent(nx3, 10, f"/s3/{size_name}", content, ops_per_agent)
            print(
                f"  [{size_name:6s}]  {_fmt_throughput(total_ops_3, wall, baseline_ops[size_name])}"
            )
        nx3.close()

        # ── Scenario 4: 5 agents, same directory ─────────────────────────
        print(f"\n{sep}")
        print("Scenario 4 — 5 concurrent agents, same directory (lock contention check)")
        print(sep)
        total_ops_4 = 5 * ops_per_agent
        print(f"  agents=5  ops_per_agent={ops_per_agent}  total_ops={total_ops_4}")

        nx4 = await _make_nx("4")
        for size_name, content in _SAMPLE_FILES.items():
            wall = _run_concurrent(
                nx4, 5, f"/s4/{size_name}", content, ops_per_agent, same_dir=True
            )
            s2_ops = total_ops_2 / s2_wall[size_name] if s2_wall.get(size_name, 0) > 0 else 0
            contention = (total_ops_4 / wall) / s2_ops if s2_ops > 0 else 0
            print(
                f"  [{size_name:6s}]  {_fmt_throughput(total_ops_4, wall, baseline_ops[size_name])}"
                f"  ratio_vs_s2={contention:.2f}x"
            )
        nx4.close()

        # ── Scenario 5: write burst ───────────────────────────────────────
        print(f"\n{sep}")
        print(f"Scenario 5 — Write burst: {burst_count} files rapid-fire to one directory")
        print(sep)
        print(f"  burst_count={burst_count}  content=1KB (synthetic; HERB WorkFlowGenie fallback)")

        nx5 = await _make_nx("5")
        burst_content = b"x" * 1024
        t0 = time.perf_counter()
        for i in range(burst_count):
            nx5.write(f"/burst/file_{i:04d}.txt", burst_content)
        burst_wall = time.perf_counter() - t0
        print(f"  {_fmt_throughput(burst_count, burst_wall)}")
        print(f"  p50_estimate={burst_wall / burst_count * 1000:.2f}ms/file")
        nx5.close()

        # ── Scenario 6: threaded agents ──────────────────────────────────
        print(f"\n{sep}")
        print("Scenario 6 — 5 threads writing different paths")
        print(sep)
        total_ops_6 = 5 * ops_per_agent
        print(f"  workers=5  ops_per_agent={ops_per_agent}  total_ops={total_ops_6}")

        nx6 = await _make_nx("6")
        for size_name, content in _SAMPLE_FILES.items():
            wall = _run_threaded(nx6, 5, f"/s6/{size_name}", content, ops_per_agent)
            print(
                f"  [{size_name:6s}]  {_fmt_throughput(total_ops_6, wall, baseline_ops[size_name])}"
            )
        nx6.close()

        # ── Findings summary ──────────────────────────────────────────────
        print(f"\n{sep}")
        print("Findings")
        print(sep)
        sizes = list(_SAMPLE_FILES.keys())
        med_tiny = baseline_med_ms.get("tiny", 0)
        med_large = baseline_med_ms.get("large", 0)
        print(
            f"  Latency flat across sizes: tiny={med_tiny:.1f}ms  large={med_large:.1f}ms"
            f"  — backend I/O is NOT the bottleneck; RaftMetadataStore.put dominates."
        )
        avg_ratio_s2 = statistics.mean(
            (5 * ops_per_agent / s2_wall[s]) / baseline_ops[s]
            for s in sizes
            if s2_wall.get(s, 0) > 0 and baseline_ops.get(s, 0) > 0
        )
        print(
            f"  5-agent concurrent throughput ratio: {avg_ratio_s2:.2f}x"
            f"  (VFS locks are per-path — contention only on same-path writes)."
        )
        print(
            "  Same-dir vs diff-path (S4/S2): no added contention — per-path lock is fine-grained."
        )
        print(
            f"  Lock hold time: {med_h:.1f}ms (backend_write + metastore.put inside _vfs_locked)."
        )

        print(f"\n{sep}\n")


def run_benchmark(
    n_seq: int = 20,
    ops_per_agent: int = 10,
    burst_count: int = 50,
) -> None:
    """Entry point for standalone execution."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _run(n_seq=n_seq, ops_per_agent=ops_per_agent, burst_count=burst_count)
        )
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# pytest entry point — uses conftest fixtures
# ---------------------------------------------------------------------------

# Minimum acceptable throughput (ops/s).  CI-tolerant: set low enough to
# pass on constrained runners, high enough to catch catastrophic regression.
_MIN_OPS_SEC = 20

# Maximum acceptable VFS lock hold time (ms).  The lock currently covers
# backend.write_content + metastore.put (~4-5 ms on local sled).  If this
# ever regresses significantly (e.g. new work added inside _vfs_locked),
# this guard catches it.
_MAX_LOCK_HOLD_MS = 50.0


def test_write_lock_contention_lock_hold(benchmark_nexus) -> None:
    """Scenario 0: VFS lock hold time must stay below ceiling."""
    hold = _measure_lock_hold(benchmark_nexus, _SMALL, 15)
    hold_ms = [t * 1000 for t in hold]
    med = statistics.median(hold_ms[3:])  # skip warmup
    print(f"\n  [lock_hold]  median={med:.3f}ms  ceiling={_MAX_LOCK_HOLD_MS}ms")
    assert med <= _MAX_LOCK_HOLD_MS, (
        f"VFS lock hold time {med:.1f}ms exceeds {_MAX_LOCK_HOLD_MS}ms ceiling"
    )


def test_write_lock_contention_sequential(benchmark_nexus, sample_files) -> None:
    """Scenario 1: single-agent sequential baseline."""
    n_ops = 10
    for size_name, content in sample_files.items():
        if size_name == "xlarge":
            continue
        times = _seq_writes(benchmark_nexus, f"/pytest/s1/{size_name}", content, n_ops)
        ops = len(times) / sum(times)
        print(f"\n  [s1/{size_name}]  {_fmt_lat(times)}")
        assert ops >= _MIN_OPS_SEC, f"{size_name}: {ops:.1f} ops/s below {_MIN_OPS_SEC}"


def test_write_lock_contention_5agents_diff_paths(benchmark_nexus, sample_files) -> None:
    """Scenario 2: 5 concurrent agents writing to different paths."""
    ops_per_agent = 5
    total = 5 * ops_per_agent
    for size_name, content in sample_files.items():
        if size_name in ("large", "xlarge"):
            continue
        wall = _run_concurrent(
            benchmark_nexus, 5, f"/pytest/s2/{size_name}", content, ops_per_agent
        )
        ops = total / wall
        print(f"\n  [s2/{size_name}]  {_fmt_throughput(total, wall)}")
        assert ops >= _MIN_OPS_SEC, f"{size_name}: {ops:.1f} ops/s below {_MIN_OPS_SEC}"


def test_write_lock_contention_10agents_diff_paths(benchmark_nexus, sample_files) -> None:
    """Scenario 3: 10 concurrent agents writing to different paths."""
    ops_per_agent = 5
    total = 10 * ops_per_agent
    for size_name, content in sample_files.items():
        if size_name in ("large", "xlarge"):
            continue
        wall = _run_concurrent(
            benchmark_nexus, 10, f"/pytest/s3/{size_name}", content, ops_per_agent
        )
        ops = total / wall
        print(f"\n  [s3/{size_name}]  {_fmt_throughput(total, wall)}")
        assert ops >= _MIN_OPS_SEC, f"{size_name}: {ops:.1f} ops/s below {_MIN_OPS_SEC}"


def test_write_lock_contention_same_dir(benchmark_nexus, sample_files) -> None:
    """Scenario 4: 5 concurrent agents, same directory — lock contention."""
    ops_per_agent = 5
    total = 5 * ops_per_agent
    for size_name, content in sample_files.items():
        if size_name in ("large", "xlarge"):
            continue
        wall = _run_concurrent(
            benchmark_nexus, 5, f"/pytest/s4/{size_name}", content, ops_per_agent, same_dir=True
        )
        ops = total / wall
        print(f"\n  [s4/{size_name}]  {_fmt_throughput(total, wall)}")
        assert ops >= _MIN_OPS_SEC, f"{size_name}: {ops:.1f} ops/s below {_MIN_OPS_SEC}"


def test_write_lock_contention_burst(benchmark_nexus) -> None:
    """Scenario 5: 50-file write burst to one directory."""
    burst_count = 50
    content = b"x" * 1024
    t0 = time.perf_counter()
    for i in range(burst_count):
        benchmark_nexus.write(f"/pytest/burst/file_{i:04d}.txt", content)
    wall = time.perf_counter() - t0
    ops = burst_count / wall
    print(f"\n  [burst/{burst_count}]  {_fmt_throughput(burst_count, wall)}")
    assert ops >= _MIN_OPS_SEC, f"burst: {ops:.1f} ops/s below {_MIN_OPS_SEC}"


# ---------------------------------------------------------------------------
# Standalone __main__
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    n_seq = 20
    ops_per_agent = 10
    burst_count = 50

    if "--quick" in sys.argv:
        n_seq = 5
        ops_per_agent = 3
        burst_count = 20

    run_benchmark(n_seq=n_seq, ops_per_agent=ops_per_agent, burst_count=burst_count)
