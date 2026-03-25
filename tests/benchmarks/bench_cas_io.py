"""CAS I/O Benchmark — measure write/read latency vs raw OS.

Compares:
  1. Raw OS write (open + write + fsync + close)
  2. CAS write_content (hash + dedup check + direct write)
  3. Raw OS read (open + read + close)
  4. CAS read_content (hash verification + read)

Usage:
    python -m pytest tests/benchmarks/bench_cas_io.py -s -v
    python tests/benchmarks/bench_cas_io.py   # standalone
"""

from __future__ import annotations

import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any


def _bench_raw_os_write(tmp_dir: Path, data: bytes, iterations: int) -> list[float]:
    """Raw OS write: open → write → fsync → close."""
    times = []
    for i in range(iterations):
        path = str(tmp_dir / f"raw_{i}")
        t0 = time.perf_counter()
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        times.append(time.perf_counter() - t0)
    return times


def _bench_raw_os_read(tmp_dir: Path, iterations: int) -> list[float]:
    """Raw OS read: open → read → close."""
    # Prepare files first
    data = os.urandom(4096)
    paths = []
    for i in range(iterations):
        path = str(tmp_dir / f"read_{i}")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)

    times = []
    for path in paths:
        t0 = time.perf_counter()
        with open(path, "rb") as f:
            _ = f.read()
        times.append(time.perf_counter() - t0)
    return times


def _bench_cas_write(engine: Any, iterations: int) -> list[float]:
    """CAS write_content: hash + dedup check + direct write."""
    times = []
    for _i in range(iterations):
        data = os.urandom(4096)  # unique each time to avoid dedup
        t0 = time.perf_counter()
        engine.write_content(data)
        times.append(time.perf_counter() - t0)
    return times


def _bench_cas_read(engine: Any, content_ids: list[str]) -> list[float]:
    """CAS read_content: retrieve by hash."""
    times = []
    for cid in content_ids:
        t0 = time.perf_counter()
        engine.read_content(cid)
        times.append(time.perf_counter() - t0)
    return times


def _format_results(label: str, times_us: list[float]) -> str:
    med = statistics.median(times_us)
    p95 = sorted(times_us)[int(len(times_us) * 0.95)]
    avg = statistics.mean(times_us)
    mn = min(times_us)
    return (
        f"  {label:30s}  median={med:8.0f}μs  p95={p95:8.0f}μs  avg={avg:8.0f}μs  min={mn:8.0f}μs"
    )


def run_benchmark(iterations: int = 500) -> None:
    from nexus import CASLocalBackend

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        data_dir = tmp_dir / "cas_data"
        raw_dir = tmp_dir / "raw_data"
        raw_dir.mkdir()

        engine = CASLocalBackend(str(data_dir))
        data_4k = os.urandom(4096)

        # Warmup
        for _ in range(50):
            engine.write_content(os.urandom(4096))
        for _ in range(50):
            p = str(raw_dir / f"warmup_{_}")
            fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
            os.write(fd, data_4k)
            os.fsync(fd)
            os.close(fd)

        print(f"\n{'=' * 72}")
        print(f"CAS I/O Benchmark — {iterations} iterations, 4KB payload")
        print(f"{'=' * 72}")

        # === WRITE ===
        print("\n--- WRITE ---")
        raw_write = _bench_raw_os_write(raw_dir, data_4k, iterations)
        cas_write = _bench_cas_write(engine, iterations)

        raw_write_us = [t * 1e6 for t in raw_write]
        cas_write_us = [t * 1e6 for t in cas_write]

        print(_format_results("Raw OS write (fsync)", raw_write_us))
        print(_format_results("CAS write_content", cas_write_us))

        ratio_write = statistics.median(cas_write_us) / statistics.median(raw_write_us)
        print(f"  {'CAS/OS ratio':30s}  {ratio_write:.1f}x")

        # === READ ===
        print("\n--- READ ---")
        # Prepare CAS content for reads
        content_ids = []
        for _i in range(iterations):
            r = engine.write_content(os.urandom(4096))
            content_ids.append(r.content_id)

        raw_read = _bench_raw_os_read(raw_dir, iterations)
        cas_read = _bench_cas_read(engine, content_ids)

        raw_read_us = [t * 1e6 for t in raw_read]
        cas_read_us = [t * 1e6 for t in cas_read]

        print(_format_results("Raw OS read", raw_read_us))
        print(_format_results("CAS read_content", cas_read_us))

        ratio_read = statistics.median(cas_read_us) / statistics.median(raw_read_us)
        print(f"  {'CAS/OS ratio':30s}  {ratio_read:.1f}x")

        # === DEDUP WRITE ===
        print("\n--- DEDUP WRITE (same content) ---")
        fixed_data = b"x" * 4096
        engine.write_content(fixed_data)  # first write
        dedup_times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            engine.write_content(fixed_data)
            dedup_times.append(time.perf_counter() - t0)

        dedup_us = [t * 1e6 for t in dedup_times]
        print(_format_results("CAS dedup write (skip blob)", dedup_us))

        print(f"\n{'=' * 72}\n")


def test_cas_io_benchmark() -> None:
    """pytest entry point — run with -s to see output."""
    run_benchmark(iterations=200)


if __name__ == "__main__":
    run_benchmark(iterations=500)
