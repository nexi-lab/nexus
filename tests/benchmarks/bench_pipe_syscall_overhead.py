#!/usr/bin/env python3
"""Benchmark: sys_write(DT_PIPE) overhead vs direct pipe_write_nowait (#1772).

Measures the cost of routing a DT_PIPE write through the kernel syscall path
(metastore lookup, path validation, dispatch resolution) vs calling
PipeManager.pipe_write_nowait() directly.

Three benchmarks:
  [1] pm.pipe_write_nowait()       — direct (current production path)
  [2] nx.sys_write(pipe_path, ..)  — full syscall (proposed migration path)
  [3] Component breakdown          — isolate each overhead source

Run:
  uv run python tests/benchmarks/bench_pipe_syscall_overhead.py
  uv run python tests/benchmarks/bench_pipe_syscall_overhead.py --json
"""

import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────
WARMUP = 200
ITERATIONS = 5000
_BENCH_PIPE_PATH = "/nexus/pipes/bench-overhead"
_BENCH_PIPE_CAPACITY = 4 * 1024 * 1024  # 4MB — won't fill during tight loop


def _percentile(data: list[float], p: float) -> float:
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])


def _fmt(us: float) -> str:
    if us < 1:
        return f"{us * 1000:.0f}ns"
    if us < 1000:
        return f"{us:.2f}us"
    return f"{us / 1000:.3f}ms"


def _stats(times_us: list[float]) -> dict:
    s = sorted(times_us)
    return {
        "mean_us": statistics.mean(s),
        "p50_us": _percentile(s, 50),
        "p95_us": _percentile(s, 95),
        "p99_us": _percentile(s, 99),
        "min_us": s[0],
        "max_us": s[-1],
    }


def _print_stats(label: str, idx: int, st: dict) -> None:
    print(f"\n[{idx}] {label}")
    print(f"  Mean: {_fmt(st['mean_us']):>10s}   P50: {_fmt(st['p50_us']):>10s}")
    print(f"  P95:  {_fmt(st['p95_us']):>10s}   P99: {_fmt(st['p99_us']):>10s}")
    print(f"  Min:  {_fmt(st['min_us']):>10s}   Max: {_fmt(st['max_us']):>10s}")


# ── Setup ──────────────────────────────────────────────────────────────


def _setup(tmp_dir: Path):
    """Create NexusFS + PipeManager with a single pipe for benchmarking."""
    from nexus.core.config import ParseConfig
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.pipe_manager import PipeManager
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    raft_path = tmp_dir / "raft"

    metastore = RaftMetadataStore.embedded(str(raft_path))
    pipe_manager = PipeManager(metastore)

    nx = NexusFS(
        metadata_store=metastore,
        is_admin=True,
        parsing=ParseConfig(auto_parse=False),
    )

    # Create the benchmark pipe
    pipe_manager.create(_BENCH_PIPE_PATH, capacity=_BENCH_PIPE_CAPACITY, owner_id="bench")

    # Register pipe inode in metastore so sys_write's metadata.get() finds it
    from nexus.contracts.metadata import DT_PIPE, FileMetadata

    metastore.put(
        FileMetadata(
            path=_BENCH_PIPE_PATH,
            backend_name="pipe",
            physical_path="",
            size=0,
            etag="",
            mime_type="application/octet-stream",
            entry_type=DT_PIPE,
            zone_id="bench",
        )
    )

    # Inject pipe_manager into NexusFS (it needs this for _pipe_write)
    nx._pipe_manager = pipe_manager

    return nx, pipe_manager, metastore


# ── Benchmark functions ────────────────────────────────────────────────


def _bench_direct(pm, data: bytes) -> list[float]:
    """[1] Direct pipe_write_nowait — current production path."""
    # warmup
    for _ in range(WARMUP):
        pm.pipe_write_nowait(_BENCH_PIPE_PATH, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        pm.pipe_write_nowait(_BENCH_PIPE_PATH, data)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)  # us
    return times


async def _bench_sys_write(nx, data: bytes) -> list[float]:
    """[2] nx.sys_write(pipe_path, data) — full syscall path."""
    # warmup
    for _ in range(WARMUP):
        await nx.sys_write(_BENCH_PIPE_PATH, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        await nx.sys_write(_BENCH_PIPE_PATH, data)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_metastore_get(metastore, path: str) -> list[float]:
    """[3a] Isolated metastore.get() — the main suspect."""
    for _ in range(WARMUP):
        metastore.get(path)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        metastore.get(path)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_validate_path(nx, path: str) -> list[float]:
    """[3b] Isolated _validate_path()."""
    for _ in range(WARMUP):
        nx._validate_path(path)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        nx._validate_path(path)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_resolve_write(nx, path: str, data: bytes) -> list[float]:
    """[3c] Isolated dispatch.resolve_write() — trie lookup."""
    for _ in range(WARMUP):
        nx._dispatch.resolve_write(path, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        nx._dispatch.resolve_write(path, data)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_check_zone_writable(nx) -> list[float]:
    """[3d] Isolated _check_zone_writable()."""
    for _ in range(WARMUP):
        nx._check_zone_writable(None)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        nx._check_zone_writable(None)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_dict_in(pm, path: str) -> list[float]:
    """[3e] `path in pm._buffers` — proposed fast-path check."""
    buffers = pm._buffers
    for _ in range(WARMUP):
        _ = path in buffers

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        _ = path in buffers
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_ideal_fast_path(pm, path: str, data: bytes) -> list[float]:
    """[4] Ideal fast-path: dict check + pipe_write_nowait (no validation/metastore)."""
    buffers = pm._buffers
    for _ in range(WARMUP):
        if path in buffers:
            pm.pipe_write_nowait(path, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        if path in buffers:
            pm.pipe_write_nowait(path, data)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


# ── Main ───────────────────────────────────────────────────────────────


async def _run() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        nx, pm, metastore = _setup(tmp_dir)

        payload = json.dumps(
            {
                "op": "write",
                "path": "/test/file.txt",
                "is_new": True,
                "zone_id": "bench",
            }
        ).encode()  # ~80 bytes, typical audit event

        # Run benchmarks
        direct = _bench_direct(pm, payload)
        sys_write = await _bench_sys_write(nx, payload)
        meta_get = _bench_metastore_get(metastore, _BENCH_PIPE_PATH)
        validate = _bench_validate_path(nx, _BENCH_PIPE_PATH)
        resolve = _bench_resolve_write(nx, _BENCH_PIPE_PATH, payload)
        zone_check = _bench_check_zone_writable(nx)
        dict_in = _bench_dict_in(pm, _BENCH_PIPE_PATH)
        fast_path = _bench_ideal_fast_path(pm, _BENCH_PIPE_PATH, payload)
        sys_write_opt = await _bench_sys_write(nx, payload)

        pm.close_all()

    return {
        "direct_pipe_write": _stats(direct),
        "sys_write": _stats(sys_write),
        "metastore_get": _stats(meta_get),
        "validate_path": _stats(validate),
        "resolve_write": _stats(resolve),
        "check_zone_writable": _stats(zone_check),
        "dict_in": _stats(dict_in),
        "fast_path": _stats(fast_path),
        "sys_write_optimized": _stats(sys_write_opt),
    }


def main() -> None:
    json_mode = "--json" in sys.argv
    results = asyncio.run(_run())

    if json_mode:
        print(json.dumps({"iterations": ITERATIONS, "warmup": WARMUP, **results}, indent=2))
        return

    print("=" * 70)
    print(f"PIPE SYSCALL OVERHEAD BENCHMARK ({ITERATIONS} iterations, {WARMUP} warmup)")
    print("=" * 70)

    _print_stats("pm.pipe_write_nowait() — direct", 1, results["direct_pipe_write"])
    _print_stats("nx.sys_write(pipe_path) — full syscall", 2, results["sys_write"])

    overhead = results["sys_write"]["mean_us"] - results["direct_pipe_write"]["mean_us"]
    print(f"\n  >>> Overhead: {_fmt(overhead)} per call")

    print("\n--- Component breakdown ---")
    _print_stats("metastore.get(path)", "3a", results["metastore_get"])
    _print_stats("_validate_path()", "3b", results["validate_path"])
    _print_stats("_dispatch.resolve_write()", "3c", results["resolve_write"])
    _print_stats("_check_zone_writable()", "3d", results["check_zone_writable"])

    component_sum = (
        results["metastore_get"]["mean_us"]
        + results["validate_path"]["mean_us"]
        + results["resolve_write"]["mean_us"]
        + results["check_zone_writable"]["mean_us"]
    )
    print(f"\n  >>> Component sum: {_fmt(component_sum)}")

    print("\n--- Proposed optimization ---")
    _print_stats("dict `in` check (pipe registry)", "3e", results["dict_in"])
    _print_stats("dict check + pipe_write_nowait (ideal)", 4, results["fast_path"])

    delta = results["fast_path"]["mean_us"] - results["direct_pipe_write"]["mean_us"]
    print(f"\n  >>> Ideal fast-path overhead vs direct: {_fmt(delta)}")

    print("\n--- After optimization ---")
    _print_stats("nx.sys_write(pipe_path) — with fast-path", 5, results["sys_write_optimized"])

    opt_delta = results["sys_write_optimized"]["mean_us"] - results["direct_pipe_write"]["mean_us"]
    print(f"\n  >>> Optimized sys_write overhead vs direct: {_fmt(opt_delta)}")
    print(
        f"  >>> Speedup: {results['sys_write']['mean_us'] / max(results['sys_write_optimized']['mean_us'], 0.001):.0f}x"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
