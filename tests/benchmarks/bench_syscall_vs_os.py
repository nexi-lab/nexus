#!/usr/bin/env python3
"""Benchmark: Nexus Rust kernel syscalls vs host OS syscalls.

First post-Rust-migration benchmark. Measures end-to-end latency of the
Nexus VFS hot paths and compares them to equivalent host OS operations.

Design philosophy:
  - Nexus syscalls go through: Python → PyO3 → Rust kernel → backend (redb/CAS)
  - Host OS syscalls go through: Python → libc → kernel → filesystem
  - We expect Nexus to be slower on DT_REG I/O (extra VFS layer + CAS hash),
    but competitive or faster on DT_PIPE/DT_STREAM (in-process, no kernel crossing)

Benchmark matrix:
  ┌──────────────────────┬──────────────────────┬─────────────────────────────┐
  │     Operation        │   Nexus syscall      │     Host OS baseline        │
  ├──────────────────────┼──────────────────────┼─────────────────────────────┤
  │ stat (metadata)      │ kernel.sys_stat      │ os.stat                     │
  │ read 1KB             │ kernel.sys_read      │ os.read                     │
  │ read 64KB            │ kernel.sys_read      │ os.read                     │
  │ read 1MB             │ kernel.sys_read      │ os.read                     │
  │ write 1KB (new)      │ kernel.sys_write     │ os.write (O_CREAT)          │
  │ write 1KB (overwrite)│ kernel.sys_write     │ os.write (O_TRUNC)          │
  │ readdir 100 entries  │ kernel.sys_readdir   │ os.scandir                  │
  │ unlink               │ kernel.sys_unlink    │ os.unlink                   │
  │ pipe write 80B       │ DT_PIPE write        │ os.write(pipe_fd)           │
  │ pipe read  80B       │ DT_PIPE read         │ os.read(pipe_fd)            │
  │ rename               │ kernel.sys_rename    │ os.rename                   │
  └──────────────────────┴──────────────────────┴─────────────────────────────┘

Run:
  uv run python tests/benchmarks/bench_syscall_vs_os.py
  uv run python tests/benchmarks/bench_syscall_vs_os.py --json
"""

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────
WARMUP = 200
ITERATIONS = 2000
PIPE_CAPACITY = 4 * 1024 * 1024  # 4MB

# Payloads
PAYLOAD_1KB = b"x" * 1024
PAYLOAD_64KB = b"y" * (64 * 1024)
PAYLOAD_1MB = b"z" * (1024 * 1024)
PIPE_PAYLOAD = json.dumps(
    {"op": "write", "path": "/test/file.txt", "is_new": True, "zone_id": "bench"}
).encode()  # ~80 bytes


# ── Stats helpers ─────────────────────────────────────────────────────


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
        return f"{us:.1f}us"
    return f"{us / 1000:.2f}ms"


def _stats(times_us: list[float]) -> dict:
    s = sorted(times_us)
    return {
        "mean_us": round(statistics.mean(s), 2),
        "p50_us": round(_percentile(s, 50), 2),
        "p95_us": round(_percentile(s, 95), 2),
        "p99_us": round(_percentile(s, 99), 2),
        "min_us": round(s[0], 2),
        "max_us": round(s[-1], 2),
        "stdev_us": round(statistics.stdev(s), 2) if len(s) > 1 else 0,
    }


def _timed_loop(func, warmup=WARMUP, iters=ITERATIONS) -> list[float]:
    """Run func() with warmup, then time `iters` iterations."""
    for _ in range(warmup):
        func()
    times: list[float] = []
    for _ in range(iters):
        t0 = time.perf_counter()
        func()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1_000_000)
    return times


# ── Setup ──────────────────────────────────────────────────────────────


def _setup_nexus(tmp_dir: Path):
    """Create NexusFS with Rust kernel + PathLocal backend (cluster profile)."""
    from nexus.backends.storage.path_local import PathLocalBackend
    from nexus.core.config import ParseConfig, PermissionConfig
    from nexus.factory import create_nexus_fs

    data_dir = tmp_dir / "nexus_data"
    data_dir.mkdir(exist_ok=True)
    raft_path = str(tmp_dir / "raft")

    nx = create_nexus_fs(
        backend=PathLocalBackend(root_path=str(data_dir)),
        metadata_store=raft_path,
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=False),
        is_admin=True,
    )
    return nx


def _setup_os_files(tmp_dir: Path, file_sizes: dict[str, bytes]):
    """Create host OS files for baseline benchmarks."""
    os_dir = tmp_dir / "os_baseline"
    os_dir.mkdir(exist_ok=True)

    paths = {}
    for name, content in file_sizes.items():
        p = os_dir / f"test_{name}.bin"
        p.write_bytes(content)
        paths[name] = str(p)

    # Create 100 files for readdir benchmark
    many_dir = os_dir / "many_files"
    many_dir.mkdir(exist_ok=True)
    for i in range(100):
        (many_dir / f"file_{i:04d}.txt").write_bytes(f"Content {i}".encode())

    paths["many_dir"] = str(many_dir)
    paths["base_dir"] = str(os_dir)
    return paths


def _setup_os_pipe():
    """Create a host OS pipe for baseline pipe benchmarks."""
    r_fd, w_fd = os.pipe()
    # Set non-blocking on read side? No — we want blocking comparison.
    return r_fd, w_fd


# ── Nexus benchmarks ──────────────────────────────────────────────────


def bench_nexus_stat(nx, path: str) -> list[float]:
    return _timed_loop(lambda: nx.sys_stat(path))


def bench_nexus_read(nx, path: str) -> list[float]:
    return _timed_loop(lambda: nx.sys_read(path))


def bench_nexus_write_new(nx) -> list[float]:
    counter = [0]

    def _write():
        counter[0] += 1
        nx.write(f"/bench_new_{counter[0]}.txt", PAYLOAD_1KB)

    return _timed_loop(_write)


def bench_nexus_write_overwrite(nx, path: str) -> list[float]:
    return _timed_loop(lambda: nx.write(path, PAYLOAD_1KB))


def bench_nexus_readdir(nx, path: str) -> list[float]:
    return _timed_loop(lambda: list(nx.sys_readdir(path, recursive=False, details=False)))


def bench_nexus_unlink(nx) -> list[float]:
    counter = [0]

    def _unlink():
        counter[0] += 1
        p = f"/bench_del_{counter[0]}.txt"
        nx.write(p, b"x")
        # Only time the unlink
        pass

    # Pre-create files
    for i in range(WARMUP + ITERATIONS + 100):
        nx.write(f"/bench_del_{i}.txt", b"x")

    idx = [0]

    def _do_unlink():
        idx[0] += 1
        nx.sys_unlink(f"/bench_del_{idx[0]}.txt")

    return _timed_loop(_do_unlink)


def bench_nexus_rename(nx) -> list[float]:
    # Pre-create files
    for i in range(WARMUP + ITERATIONS + 100):
        nx.write(f"/bench_ren_src_{i}.txt", b"x")

    idx = [0]

    def _rename():
        idx[0] += 1
        nx.sys_rename(f"/bench_ren_src_{idx[0]}.txt", f"/bench_ren_dst_{idx[0]}.txt")

    return _timed_loop(_rename)


def bench_nexus_pipe_roundtrip(nx) -> list[float]:
    """Write then immediately read — measures full pipe roundtrip.

    We always pair write+read so the ring buffer never fills.
    This is the only safe way to benchmark pipe throughput in a
    single-threaded tight loop.
    """
    pipe_path = "/bench/pipe_rt"
    from nexus.contracts.metadata import DT_PIPE

    nx.sys_setattr(pipe_path, entry_type=DT_PIPE, capacity=PIPE_CAPACITY, owner_id="bench")
    kernel = nx._kernel

    def _roundtrip():
        kernel.pipe_write_nowait(pipe_path, PIPE_PAYLOAD)
        kernel.pipe_read_nowait(pipe_path)

    return _timed_loop(_roundtrip)


# ── Host OS benchmarks ────────────────────────────────────────────────


def bench_os_stat(path: str) -> list[float]:
    return _timed_loop(lambda: os.stat(path))


def bench_os_read(path: str, size: int) -> list[float]:
    fd = os.open(path, os.O_RDONLY)

    def _read():
        os.lseek(fd, 0, os.SEEK_SET)
        os.read(fd, size)

    result = _timed_loop(_read)
    os.close(fd)
    return result


def bench_os_write_new(base_dir: str) -> list[float]:
    counter = [0]

    def _write():
        counter[0] += 1
        p = os.path.join(base_dir, f"bench_new_{counter[0]}.txt")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.write(fd, PAYLOAD_1KB)
        os.close(fd)

    return _timed_loop(_write)


def bench_os_write_overwrite(path: str) -> list[float]:
    def _write():
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        os.write(fd, PAYLOAD_1KB)
        os.close(fd)

    return _timed_loop(_write)


def bench_os_readdir(path: str) -> list[float]:
    return _timed_loop(lambda: list(os.scandir(path)))


def bench_os_unlink(base_dir: str) -> list[float]:
    # Pre-create files
    for i in range(WARMUP + ITERATIONS + 100):
        p = os.path.join(base_dir, f"bench_del_{i}.txt")
        with open(p, "wb") as f:
            f.write(b"x")

    idx = [0]

    def _unlink():
        idx[0] += 1
        os.unlink(os.path.join(base_dir, f"bench_del_{idx[0]}.txt"))

    return _timed_loop(_unlink)


def bench_os_rename(base_dir: str) -> list[float]:
    for i in range(WARMUP + ITERATIONS + 100):
        p = os.path.join(base_dir, f"bench_ren_src_{i}.txt")
        with open(p, "wb") as f:
            f.write(b"x")

    idx = [0]

    def _rename():
        idx[0] += 1
        os.rename(
            os.path.join(base_dir, f"bench_ren_src_{idx[0]}.txt"),
            os.path.join(base_dir, f"bench_ren_dst_{idx[0]}.txt"),
        )

    return _timed_loop(_rename)


def bench_os_pipe_write(w_fd: int) -> list[float]:
    return _timed_loop(lambda: os.write(w_fd, PIPE_PAYLOAD))


def bench_os_pipe_roundtrip(r_fd: int, w_fd: int) -> list[float]:
    def _roundtrip():
        os.write(w_fd, PIPE_PAYLOAD)
        os.read(r_fd, len(PIPE_PAYLOAD))

    return _timed_loop(_roundtrip)


# ── Main ───────────────────────────────────────────────────────────────

_COL_W = 28  # operation column width
_NUM_W = 10  # number column width


def _print_header():
    ops = "Operation".ljust(_COL_W)
    nexus_h = "Nexus".center(_NUM_W)
    os_h = "Host OS".center(_NUM_W)
    ratio_h = "Ratio".center(_NUM_W)
    p95_h = "Nx P95".center(_NUM_W)
    print(f"  {ops}  {nexus_h}  {os_h}  {ratio_h}  {p95_h}")
    print(f"  {'─' * _COL_W}  {'─' * _NUM_W}  {'─' * _NUM_W}  {'─' * _NUM_W}  {'─' * _NUM_W}")


def _print_row(label: str, nx_st: dict, os_st: dict):
    ratio = nx_st["p50_us"] / os_st["p50_us"] if os_st["p50_us"] > 0 else float("inf")
    # Color: green if ratio < 2, yellow if < 5, red if >= 5
    if ratio <= 1.5:
        tag = "  ✓"
    elif ratio <= 5:
        tag = "   "
    else:
        tag = " !!"

    ops = label.ljust(_COL_W)
    nx_val = _fmt(nx_st["p50_us"]).rjust(_NUM_W)
    os_val = _fmt(os_st["p50_us"]).rjust(_NUM_W)
    ratio_str = f"{ratio:.1f}x".rjust(_NUM_W)
    p95_str = _fmt(nx_st["p95_us"]).rjust(_NUM_W)
    print(f"{tag} {ops}  {nx_val}  {os_val}  {ratio_str}  {p95_str}")


def _run():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        print("Setting up Nexus kernel...")
        nx = _setup_nexus(tmp_dir)
        kernel = nx._kernel

        file_sizes = {
            "1kb": PAYLOAD_1KB,
            "64kb": PAYLOAD_64KB,
            "1mb": PAYLOAD_1MB,
        }
        os_paths = _setup_os_files(tmp_dir, file_sizes)

        # Pre-populate Nexus with test files
        for name, content in file_sizes.items():
            nx.write(f"/test_{name}.bin", content)
        nx.write("/bench_overwrite.txt", PAYLOAD_1KB)
        # Create 100 files for readdir
        nx.mkdir("/many_files", parents=True)
        for i in range(100):
            nx.write(f"/many_files/file_{i:04d}.txt", f"Content {i}".encode())

        # Set up OS pipe
        r_fd, w_fd = _setup_os_pipe()
        kernel = nx._kernel

        results = {}

        print(f"Running benchmarks ({ITERATIONS} iterations, {WARMUP} warmup)...\n")

        # ── stat ──
        print("  [1/12] stat ...")
        nx_stat = bench_nexus_stat(nx, "/test_1kb.bin")
        os_stat = bench_os_stat(os_paths["1kb"])
        results["stat"] = {"nexus": _stats(nx_stat), "os": _stats(os_stat)}

        # ── read 1KB ──
        print("  [2/12] read 1KB ...")
        nx_r1k = bench_nexus_read(nx, "/test_1kb.bin")
        os_r1k = bench_os_read(os_paths["1kb"], 1024)
        results["read_1kb"] = {"nexus": _stats(nx_r1k), "os": _stats(os_r1k)}

        # ── read 64KB ──
        print("  [3/12] read 64KB ...")
        nx_r64k = bench_nexus_read(nx, "/test_64kb.bin")
        os_r64k = bench_os_read(os_paths["64kb"], 64 * 1024)
        results["read_64kb"] = {"nexus": _stats(nx_r64k), "os": _stats(os_r64k)}

        # ── read 1MB ──
        print("  [4/12] read 1MB ...")
        nx_r1m = bench_nexus_read(nx, "/test_1mb.bin")
        os_r1m = bench_os_read(os_paths["1mb"], 1024 * 1024)
        results["read_1mb"] = {"nexus": _stats(nx_r1m), "os": _stats(os_r1m)}

        # ── write new ──
        print("  [5/12] write 1KB (new file) ...")
        nx_wn = bench_nexus_write_new(nx)
        os_wn = bench_os_write_new(os_paths["base_dir"])
        results["write_new_1kb"] = {"nexus": _stats(nx_wn), "os": _stats(os_wn)}

        # ── write overwrite ──
        print("  [6/12] write 1KB (overwrite) ...")
        nx_wo = bench_nexus_write_overwrite(nx, "/bench_overwrite.txt")
        os_wo = bench_os_write_overwrite(os_paths["1kb"])
        results["write_overwrite_1kb"] = {"nexus": _stats(nx_wo), "os": _stats(os_wo)}

        # ── readdir 100 entries ──
        print("  [7/12] readdir (100 entries) ...")
        nx_rd = bench_nexus_readdir(nx, "/many_files")
        os_rd = bench_os_readdir(os_paths["many_dir"])
        results["readdir_100"] = {"nexus": _stats(nx_rd), "os": _stats(os_rd)}

        # ── unlink ──
        print("  [8/12] unlink ...")
        nx_ul = bench_nexus_unlink(nx)
        os_ul = bench_os_unlink(os_paths["base_dir"])
        results["unlink"] = {"nexus": _stats(nx_ul), "os": _stats(os_ul)}

        # ── rename ──
        print("  [9/12] rename ...")
        nx_rn = bench_nexus_rename(nx)
        os_rn = bench_os_rename(os_paths["base_dir"])
        results["rename"] = {"nexus": _stats(nx_rn), "os": _stats(os_rn)}

        # ── pipe roundtrip ──
        print("  [10/11] pipe roundtrip 80B (write+read) ...")
        nx_prt = bench_nexus_pipe_roundtrip(nx)
        os_prt = bench_os_pipe_roundtrip(r_fd, w_fd)
        results["pipe_roundtrip"] = {"nexus": _stats(nx_prt), "os": _stats(os_prt)}

        # ── stat (dcache hot) — re-run to show warm-cache perf ──
        print("  [11/11] stat (dcache hot, re-run) ...")
        nx_stat2 = bench_nexus_stat(nx, "/test_1kb.bin")
        os_stat2 = bench_os_stat(os_paths["1kb"])
        results["stat_hot"] = {"nexus": _stats(nx_stat2), "os": _stats(os_stat2)}

        # Cleanup
        os.close(r_fd)
        os.close(w_fd)
        kernel.close_all_pipes()
        nx.close()

        return results


def _print_results(results: dict):
    print()
    print("=" * 80)
    print("  NEXUS KERNEL vs HOST OS SYSCALL BENCHMARK")
    print(f"  {ITERATIONS} iterations, {WARMUP} warmup, P50 latency (lower = better)")
    print("=" * 80)

    print()
    print("  ── DT_REG (regular file I/O — Nexus has CAS hash + redb overhead) ──")
    _print_header()
    _print_row("stat", results["stat"]["nexus"], results["stat"]["os"])
    _print_row("stat (dcache hot)", results["stat_hot"]["nexus"], results["stat_hot"]["os"])
    _print_row("read 1KB", results["read_1kb"]["nexus"], results["read_1kb"]["os"])
    _print_row("read 64KB", results["read_64kb"]["nexus"], results["read_64kb"]["os"])
    _print_row("read 1MB", results["read_1mb"]["nexus"], results["read_1mb"]["os"])
    _print_row("write 1KB (new)", results["write_new_1kb"]["nexus"], results["write_new_1kb"]["os"])
    _print_row(
        "write 1KB (overwrite)",
        results["write_overwrite_1kb"]["nexus"],
        results["write_overwrite_1kb"]["os"],
    )
    _print_row("readdir (100)", results["readdir_100"]["nexus"], results["readdir_100"]["os"])
    _print_row("unlink", results["unlink"]["nexus"], results["unlink"]["os"])
    _print_row("rename", results["rename"]["nexus"], results["rename"]["os"])

    print()
    print("  ── DT_PIPE (in-process IPC — Nexus should win: no kernel crossing) ──")
    _print_header()
    _print_row(
        "pipe roundtrip 80B", results["pipe_roundtrip"]["nexus"], results["pipe_roundtrip"]["os"]
    )

    print()
    print("  Legend: ✓ = within 1.5x of OS, !! = >5x slower")
    print("  Note: OS read/write uses pre-opened fd (no open/close overhead)")
    print("  Note: OS pipe is a real kernel pipe (two fd, context switch)")
    print("  Note: Nexus pipe is in-process ring buffer (no kernel crossing)")
    print()

    # Summary stats
    reg_ops = [
        "stat",
        "stat_hot",
        "read_1kb",
        "read_64kb",
        "read_1mb",
        "write_new_1kb",
        "write_overwrite_1kb",
        "readdir_100",
        "unlink",
        "rename",
    ]
    pipe_ops = ["pipe_roundtrip"]

    reg_ratios = []
    for op in reg_ops:
        nx_p50 = results[op]["nexus"]["p50_us"]
        os_p50 = results[op]["os"]["p50_us"]
        if os_p50 > 0:
            reg_ratios.append(nx_p50 / os_p50)

    pipe_ratios = []
    for op in pipe_ops:
        nx_p50 = results[op]["nexus"]["p50_us"]
        os_p50 = results[op]["os"]["p50_us"]
        if os_p50 > 0:
            pipe_ratios.append(nx_p50 / os_p50)

    print(f"  DT_REG geometric mean ratio: {_geometric_mean(reg_ratios):.1f}x")
    print(f"  DT_PIPE geometric mean ratio: {_geometric_mean(pipe_ratios):.1f}x")
    if pipe_ratios and all(r < 1 for r in pipe_ratios):
        print("  → Nexus DT_PIPE is FASTER than OS pipe (expected: in-process vs kernel)")
    print("=" * 80)


def _geometric_mean(values: list[float]) -> float:
    if not values:
        return 0
    product = 1.0
    for v in values:
        product *= v
    return product ** (1 / len(values))


def main():
    json_mode = "--json" in sys.argv
    results = _run()

    if json_mode:
        out = {"iterations": ITERATIONS, "warmup": WARMUP, "results": results}
        print(json.dumps(out, indent=2))
    else:
        _print_results(results)


if __name__ == "__main__":
    main()
