#!/usr/bin/env python3
"""Benchmark: sys_write/sys_read(DT_PIPE) overhead vs direct pipe_write_nowait (#1772).

Measures the cost of routing DT_PIPE read/write through the kernel syscall path
(metastore lookup, path validation, dispatch resolution) vs calling
kernel.pipe_write_nowait() directly.

Benchmarks:
  [1]  kernel.pipe_write_nowait()   — direct (current production path)
  [2a] nx.sys_write(pipe_path, ..)  — full syscall write path
  [2b] nx.sys_read(pipe_path)       — full syscall read path
  [3]  Component breakdown          — isolate each overhead source

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


async def _setup(tmp_dir: Path):
    """Create NexusFS + Rust kernel pipe for benchmarking."""
    from nexus.backends.storage.path_local import PathLocalBackend
    from nexus.core.config import ParseConfig
    from nexus.factory import create_nexus_fs
    from nexus.storage.raft_metadata_store import RaftMetadataStore
    from tests.helpers.test_context import TEST_ADMIN_CONTEXT

    raft_path = tmp_dir / "raft"
    data_dir = tmp_dir / "data"
    data_dir.mkdir(exist_ok=True)

    metastore = RaftMetadataStore.embedded(str(raft_path))

    nx = await create_nexus_fs(
        backend=PathLocalBackend(root_path=str(data_dir)),
        metadata_store=metastore,
        parsing=ParseConfig(auto_parse=False),
        init_cred=TEST_ADMIN_CONTEXT,
    )

    # Create the benchmark pipe via sys_setattr (standard path)
    from nexus.contracts.metadata import DT_PIPE

    nx.sys_setattr(
        _BENCH_PIPE_PATH,
        entry_type=DT_PIPE,
        capacity=_BENCH_PIPE_CAPACITY,
        owner_id="bench",
    )

    kernel = nx._kernel

    return nx, kernel, metastore


# ── Benchmark functions ────────────────────────────────────────────────


def _bench_direct(kernel, data: bytes) -> list[float]:
    """[1] Direct kernel.pipe_write_nowait — current production path."""
    # warmup
    for _ in range(WARMUP):
        kernel.pipe_write_nowait(_BENCH_PIPE_PATH, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        kernel.pipe_write_nowait(_BENCH_PIPE_PATH, data)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)  # us
    return times


async def _bench_sys_write(nx, data: bytes) -> list[float]:
    """[2] nx.sys_write(pipe_path, data) — full syscall path."""
    # warmup
    for _ in range(WARMUP):
        nx.sys_write(_BENCH_PIPE_PATH, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        nx.sys_write(_BENCH_PIPE_PATH, data)
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
        nx.resolve_write(path, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        nx.resolve_write(path, data)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_dict_in(kernel, path: str) -> list[float]:
    """[3e] `path in kernel.list_pipes()` — proposed fast-path check."""
    for _ in range(WARMUP):
        _ = path in kernel.list_pipes()

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        _ = path in kernel.list_pipes()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


async def _bench_sys_read(nx, kernel, data: bytes) -> list[float]:
    """[2b] nx.sys_read(pipe_path) — full syscall path (pre-fill + read)."""
    # warmup
    for _ in range(WARMUP):
        kernel.pipe_write_nowait(_BENCH_PIPE_PATH, data)
        nx.sys_read(_BENCH_PIPE_PATH)

    times: list[float] = []
    for _ in range(ITERATIONS):
        kernel.pipe_write_nowait(_BENCH_PIPE_PATH, data)
        t0 = time.perf_counter()
        nx.sys_read(_BENCH_PIPE_PATH)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


def _bench_ideal_fast_path(kernel, path: str, data: bytes) -> list[float]:
    """[4] Ideal fast-path: pipe_write_nowait (no validation/metastore)."""
    for _ in range(WARMUP):
        kernel.pipe_write_nowait(path, data)

    times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        kernel.pipe_write_nowait(path, data)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


# ── Main ───────────────────────────────────────────────────────────────


async def _run() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        nx, kernel, metastore = await _setup(tmp_dir)

        payload = json.dumps(
            {
                "op": "write",
                "path": "/test/file.txt",
                "is_new": True,
                "zone_id": "bench",
            }
        ).encode()  # ~80 bytes, typical audit event

        # Run benchmarks
        direct = _bench_direct(kernel, payload)
        sys_write = await _bench_sys_write(nx, payload)
        sys_read = await _bench_sys_read(nx, kernel, payload)
        meta_get = _bench_metastore_get(metastore, _BENCH_PIPE_PATH)
        validate = _bench_validate_path(nx, _BENCH_PIPE_PATH)
        resolve = _bench_resolve_write(nx, _BENCH_PIPE_PATH, payload)
        dict_in = _bench_dict_in(kernel, _BENCH_PIPE_PATH)
        fast_path = _bench_ideal_fast_path(kernel, _BENCH_PIPE_PATH, payload)
        sys_write_opt = await _bench_sys_write(nx, payload)
        sys_read_opt = await _bench_sys_read(nx, kernel, payload)

        kernel.close_all_pipes()

    return {
        "direct_pipe_write": _stats(direct),
        "sys_write": _stats(sys_write),
        "sys_read": _stats(sys_read),
        "metastore_get": _stats(meta_get),
        "validate_path": _stats(validate),
        "resolve_write": _stats(resolve),
        "dict_in": _stats(dict_in),
        "fast_path": _stats(fast_path),
        "sys_write_optimized": _stats(sys_write_opt),
        "sys_read_optimized": _stats(sys_read_opt),
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

    _print_stats("kernel.pipe_write_nowait() — direct", 1, results["direct_pipe_write"])
    _print_stats("nx.sys_write(pipe_path) — full syscall", "2a", results["sys_write"])
    _print_stats("nx.sys_read(pipe_path) — full syscall", "2b", results["sys_read"])

    overhead = results["sys_write"]["mean_us"] - results["direct_pipe_write"]["mean_us"]
    print(f"\n  >>> sys_write overhead: {_fmt(overhead)} per call")

    print("\n--- Component breakdown ---")
    _print_stats("metastore.get(path)", "3a", results["metastore_get"])
    _print_stats("_validate_path()", "3b", results["validate_path"])
    _print_stats("_dispatch.resolve_write()", "3c", results["resolve_write"])

    component_sum = (
        results["metastore_get"]["mean_us"]
        + results["validate_path"]["mean_us"]
        + results["resolve_write"]["mean_us"]
    )
    print(f"\n  >>> Component sum: {_fmt(component_sum)}")

    print("\n--- Proposed optimization ---")
    _print_stats("list_pipes() `in` check (pipe registry)", "3e", results["dict_in"])
    _print_stats("pipe_write_nowait (ideal)", 4, results["fast_path"])

    delta = results["fast_path"]["mean_us"] - results["direct_pipe_write"]["mean_us"]
    print(f"\n  >>> Ideal fast-path overhead vs direct: {_fmt(delta)}")

    print("\n--- After optimization ---")
    _print_stats("nx.sys_write(pipe_path) — with fast-path", "5a", results["sys_write_optimized"])
    _print_stats("nx.sys_read(pipe_path) — with fast-path", "5b", results["sys_read_optimized"])

    opt_delta = results["sys_write_optimized"]["mean_us"] - results["direct_pipe_write"]["mean_us"]
    print(f"\n  >>> Optimized sys_write overhead vs direct: {_fmt(opt_delta)}")
    print(
        f"  >>> sys_write speedup: {results['sys_write']['mean_us'] / max(results['sys_write_optimized']['mean_us'], 0.001):.0f}x"
    )
    opt_read_delta = (
        results["sys_read_optimized"]["mean_us"] - results["direct_pipe_write"]["mean_us"]
    )
    print(f"  >>> Optimized sys_read overhead vs direct: {_fmt(opt_read_delta)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
