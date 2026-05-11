"""Stress smoke test — multiple PrefetchEngine instances, large workloads,
explicit + implicit shutdown paths.  Validates no GIL deadlock under load.
"""

from __future__ import annotations

import sys
import time

from nexus_runtime import PrefetchEngine


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}", flush=True)


def build_engine(file_bytes: bytes) -> PrefetchEngine:
    def reader(_p: str, offset: int, size: int) -> bytes:
        return file_bytes[offset : offset + size]

    return PrefetchEngine(
        reader,
        4096,  # block_size
        16384,  # initial_window
        262144,  # max_window 256 KiB
        4,  # max_workers
        128,  # queue_capacity
        8,  # max_blocks_per_trigger
        0,  # sequential_tolerance
        2,  # min_sequential_count
    )


def run_workload(engine: PrefetchEngine, fh: int, file_size: int, name: str) -> None:
    engine.on_open(fh, name, file_size)
    hits = 0
    misses = 0
    for i in range(file_size // 4096):
        off = i * 4096
        if i and i % 8 == 0:
            time.sleep(0.02)
        got = engine.on_read(fh, off, 4096)
        if got is not None:
            hits += 1
        else:
            misses += 1
    engine.on_release(fh)
    print(
        f"  workload={name}: hits={hits} misses={misses} ratio={hits / (hits + misses):.2f}",
        flush=True,
    )


def case_explicit_shutdown(file_bytes: bytes) -> None:
    print("\n--- case 1: explicit shutdown after large workload ---", flush=True)
    e = build_engine(file_bytes)
    run_workload(e, 1, len(file_bytes), "/A")
    e.shutdown()
    ok("explicit shutdown returned (no deadlock)")


def case_implicit_drop(file_bytes: bytes) -> None:
    print("\n--- case 2: implicit drop (no shutdown call) ---", flush=True)
    e = build_engine(file_bytes)
    run_workload(e, 2, len(file_bytes), "/B")
    # Don't call shutdown; let Python GC drop the engine.
    del e
    ok("implicit drop returned (no deadlock)")


def case_three_engines(file_bytes: bytes) -> None:
    print("\n--- case 3: three engines concurrent lifetime ---", flush=True)
    engines = [build_engine(file_bytes) for _ in range(3)]
    for i, e in enumerate(engines, start=1):
        run_workload(e, 100 + i, len(file_bytes), f"/C{i}")
    # Mixed teardown — shutdown some, drop others.
    engines[0].shutdown()
    del engines[2]
    del engines  # second engine implicit-drops
    ok("three-engine mixed teardown returned (no deadlock)")


def main() -> None:
    file_bytes = b"\x99" * (1 << 20)  # 1 MiB
    case_explicit_shutdown(file_bytes)
    case_implicit_drop(file_bytes)
    case_three_engines(file_bytes)
    print("\n=== STRESS GREEN ===", flush=True)


if __name__ == "__main__":
    main()
