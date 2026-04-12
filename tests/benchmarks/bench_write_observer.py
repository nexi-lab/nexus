#!/usr/bin/env python3
"""Benchmark: write observer hot-path latency (Issue #995).

Measures on_write() per-call latency for:
  1. RecordStoreWriteObserver (sync DB -- baseline)
  2. RecordStoreWriteObserver (OBSERVE-phase -- debounced batch)

Run:
  uv run python tests/benchmarks/bench_write_observer.py
  uv run python tests/benchmarks/bench_write_observer.py --json
"""

import json
import logging
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from nexus.contracts.metadata import FileMetadata
from nexus.core.file_events import FileEvent, FileEventType
from nexus.storage.piped_record_store_write_observer import (
    RecordStoreWriteObserver as ObserverWriteObserver,
)
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.record_store_write_observer import RecordStoreWriteObserver

# ── Constants ──────────────────────────────────────────────────────────
WARMUP = 100
ITERATIONS = 1000


def _make_metadata(path: str, *, etag: str = "abc123", size: int = 100) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=etag,
        size=size,
        etag=etag,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=1,
        zone_id="root",
        owner_id="bench",
    )


def _percentile(data: list[float], p: float) -> float:
    """Return the p-th percentile (0-100) of sorted data."""
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])


def _fmt_latency(ms: float) -> str:
    """Format latency: use us when sub-0.1ms, otherwise ms."""
    if ms < 0.1:
        return f"{ms * 1000:.1f}us"
    return f"{ms:.3f}ms"


# ── Sync observer benchmark ───────────────────────────────────────────
def bench_sync(tmp_dir: Path) -> list[float]:
    db_path = tmp_dir / "sync.db"
    record_store = SQLAlchemyRecordStore(db_path=str(db_path))
    observer = RecordStoreWriteObserver(record_store, strict_mode=False)

    # Warmup
    for i in range(WARMUP):
        md = _make_metadata(f"/warmup/{i}.txt", etag=f"w{i}")
        observer.on_write(md, is_new=True, path=md.path)

    # Measure
    times: list[float] = []
    for i in range(ITERATIONS):
        md = _make_metadata(f"/bench/{i}.txt", etag=f"b{i}")
        t0 = time.perf_counter()
        observer.on_write(md, is_new=True, path=md.path)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)  # ms

    return times


# ── OBSERVE-phase observer benchmark ─────────────────────────────────
def bench_observer(tmp_dir: Path) -> list[float]:
    db_path = tmp_dir / "observer.db"

    record_store = SQLAlchemyRecordStore(db_path=str(db_path))

    observer = ObserverWriteObserver(record_store, strict_mode=False, debounce_seconds=0.2)

    # Suppress observer warnings during benchmark
    obs_logger = logging.getLogger("nexus.storage.piped_record_store_write_observer")
    prev_level = obs_logger.level
    obs_logger.setLevel(logging.ERROR)

    try:
        # Warmup
        for i in range(WARMUP):
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path=f"/warmup/{i}.txt",
                is_new=True,
                etag=f"w{i}",
            )
            observer.on_mutation(event)

        # Wait for warmup to flush
        time.sleep(0.5)

        # Measure on_mutation latency (fire-and-forget)
        times: list[float] = []
        for i in range(ITERATIONS):
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path=f"/bench/{i}.txt",
                is_new=True,
                etag=f"b{i}",
            )
            t0 = time.perf_counter()
            observer.on_mutation(event)
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000)  # ms
    finally:
        observer.flush_sync()
        observer.cancel()
        obs_logger.setLevel(prev_level)

    return times


# ── Reporting ─────────────────────────────────────────────────────────
def _compute_stats(times: list[float]) -> dict:
    s = sorted(times)
    return {
        "mean_ms": statistics.mean(s),
        "p50_ms": _percentile(s, 50),
        "p95_ms": _percentile(s, 95),
        "p99_ms": _percentile(s, 99),
        "min_ms": s[0],
        "max_ms": s[-1],
        "wall_ms": sum(s),
        "throughput": len(s) / (sum(s) / 1000),
    }


def _print_stats(label: str, idx: int, stats: dict) -> None:
    print(f"\n[{idx}] {label}")
    print(f"  Mean:       {_fmt_latency(stats['mean_ms'])}")
    print(f"  P50:        {_fmt_latency(stats['p50_ms'])}")
    print(f"  P95:        {_fmt_latency(stats['p95_ms'])}")
    print(f"  P99:        {_fmt_latency(stats['p99_ms'])}")
    print(f"  Min:        {_fmt_latency(stats['min_ms'])}")
    print(f"  Max:        {_fmt_latency(stats['max_ms'])}")
    print(f"  Wall time:  {stats['wall_ms']:.1f}ms ({ITERATIONS} calls)")
    print(f"  Throughput: {stats['throughput']:,.0f} writes/sec")


def main() -> None:
    json_mode = "--json" in sys.argv

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # Run benchmarks
        sync_times = bench_sync(tmp_dir)
        observer_times = bench_observer(tmp_dir)

    sync_stats = _compute_stats(sync_times)
    observer_stats = _compute_stats(observer_times)
    speedup = (
        sync_stats["mean_ms"] / observer_stats["mean_ms"]
        if observer_stats["mean_ms"] > 0
        else float("inf")
    )

    if json_mode:
        print(
            json.dumps(
                {
                    "iterations": ITERATIONS,
                    "warmup": WARMUP,
                    "sync": sync_stats,
                    "observer": observer_stats,
                    "speedup": round(speedup, 1),
                },
                indent=2,
            )
        )
        return

    # Human-readable output
    print("=" * 70)
    print(f"WRITE OBSERVER BENCHMARK ({ITERATIONS} files x 1KB)")
    print("=" * 70)

    _print_stats("RecordStoreWriteObserver (sync DB)", 1, sync_stats)
    _print_stats("RecordStoreWriteObserver (OBSERVE-phase)", 2, observer_stats)

    print()
    print("=" * 70)
    print(
        f"SPEEDUP: {speedup:.0f}x "
        f"(sync={_fmt_latency(sync_stats['mean_ms'])} "
        f"\u2192 observer={_fmt_latency(observer_stats['mean_ms'])})"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
