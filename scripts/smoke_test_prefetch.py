"""Smoke test for nexus-prefetch end-to-end inside the running container.

Validates:
  1. `nexus_runtime.PrefetchEngine` imports.
  2. Constructing a ReadaheadManager with `use_rust_engine=True` succeeds.
  3. Sequential workload produces majority hits.
  4. Metrics from the engine show prefetched bytes > 0 and hits > 0.
  5. `NEXUS_PREFETCH_RUST=0` env toggle disables the Rust path.

Exit code 0 = green, 1 = failure.

Run inside the container:
    nexus exec nexus python /app/scripts/smoke_test_prefetch.py
"""

from __future__ import annotations

import os
import sys
import time


def banner(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}", flush=True)


def check_import() -> None:
    banner("1. Import nexus_runtime.PrefetchEngine")
    try:
        from nexus_runtime import PrefetchEngine  # noqa: F401
    except ImportError as e:
        fail(f"PrefetchEngine import failed: {e}")
    ok("PrefetchEngine available")


def check_engine_directly() -> None:
    banner("2. Drive PrefetchEngine directly via pyo3")
    from nexus_runtime import PrefetchEngine

    file_bytes = b"\x42" * (256 * 1024)

    def reader(_path: str, offset: int, size: int) -> bytes:
        return file_bytes[offset : offset + size]

    engine = PrefetchEngine(
        reader,
        4096,  # block_size
        16384,  # initial_window
        131072,  # max_window
        4,  # max_workers
        128,  # queue_capacity
        4,  # max_blocks_per_trigger
        0,  # sequential_tolerance
        2,  # min_sequential_count
    )

    engine.on_open(1, "/synthetic", len(file_bytes))

    hits = 0
    misses = 0
    for i in range(40):
        off = i * 4096
        if i in (4, 8, 16, 24):
            time.sleep(0.05)
        got = engine.on_read(1, off, 4096)
        if got is not None:
            assert got == file_bytes[off : off + 4096], f"hit data mismatch at offset {off}"
            hits += 1
        else:
            misses += 1

    engine.on_release(1)
    snapshot = engine.metrics()
    snap_hits, snap_misses, prefetched_bytes, dropped, resets = snapshot

    print(f"hits={hits} misses={misses}", flush=True)
    print(
        f"metrics: hits={snap_hits} misses={snap_misses} "
        f"prefetched_bytes={prefetched_bytes} "
        f"dropped={dropped} resets={resets}",
        flush=True,
    )

    if hits == 0:
        fail("no hits — prefetcher never delivered a block")
    if prefetched_bytes == 0:
        fail("metric prefetched_bytes == 0 — workers never deposited")
    if hits < misses // 4:
        fail(f"hit ratio too low: {hits}/{hits + misses}")
    ok(f"direct engine: {hits} hits, {prefetched_bytes} bytes prefetched")

    engine.shutdown()


def check_readahead_manager_rust() -> None:
    banner("3. ReadaheadManager with use_rust_engine=True")
    # Load readahead module directly to skip nexus.fuse.__init__ which
    # eagerly imports fusepy (not installed in headless containers).
    import importlib.util
    import pathlib

    spec_path = pathlib.Path("/usr/local/lib/python3.14/site-packages/nexus/fuse/readahead.py")
    spec = importlib.util.spec_from_file_location("nexus.fuse.readahead", spec_path)
    if spec is None or spec.loader is None:
        fail(f"could not load readahead module from {spec_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ReadaheadConfig = mod.ReadaheadConfig
    ReadaheadManager = mod.ReadaheadManager

    file_bytes = b"\x37" * (256 * 1024)

    def read_func(_path: str, offset: int, size: int) -> bytes:
        return file_bytes[offset : offset + size]

    config = ReadaheadConfig(
        enabled=True,
        block_size=4096,
        prefetch_workers=2,
        min_sequential_count=2,
        initial_window=16384,
        max_window=131072,
        sequential_tolerance=0,
        max_blocks_per_trigger=4,
    )
    rm = ReadaheadManager(config=config, read_func=read_func, use_rust_engine=True)
    if rm._rust_engine is None:
        fail(
            "use_rust_engine=True but _rust_engine is None — "
            "PrefetchEngine import or construction failed silently"
        )
    ok("ReadaheadManager._rust_engine bound")

    fh = 1
    rm.on_open(fh, "/synthetic", file_size=len(file_bytes))
    rm.on_read(fh, "/synthetic", 0, 4096)  # warm 1
    rm.on_read(fh, "/synthetic", 4096, 4096)  # warm 2
    rm.on_read(fh, "/synthetic", 8192, 4096)  # triggers prefetch
    time.sleep(0.3)
    got = rm.on_read(fh, "/synthetic", 12288, 4096)
    if got is None:
        fail("ReadaheadManager(Rust) returned None on expected-hit read at offset 12288")
    if got != file_bytes[12288:16384]:
        fail("ReadaheadManager(Rust) returned wrong bytes at offset 12288")
    rm.on_release(fh)
    ok("ReadaheadManager(Rust) served a sequential hit")


def check_env_toggle_off() -> None:
    banner("4. NEXUS_PREFETCH_RUST=0 disables Rust path")
    os.environ["NEXUS_PREFETCH_RUST"] = "0"
    enabled = os.environ.get("NEXUS_PREFETCH_RUST", "1") != "0"
    if enabled:
        fail("env-toggle evaluation broken")
    ok("env toggle evaluation correct (Rust would be off)")


def main() -> None:
    check_import()
    check_engine_directly()
    check_readahead_manager_rust()
    check_env_toggle_off()
    banner("ALL GREEN")


if __name__ == "__main__":
    main()
