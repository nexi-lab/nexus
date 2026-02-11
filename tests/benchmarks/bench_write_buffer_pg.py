"""Benchmark: WriteBuffer vs sync RecordStoreSyncer on real PostgreSQL.

Issue #1246 — Verify that the WriteBuffer actually delivers latency savings
when writing to PostgreSQL (network I/O is the bottleneck, not SQLite).

Requirements:
    - PostgreSQL running on localhost:5433 (docker-compose --profile test up)
    - DB: nexus_test, User: nexus_test, Password: nexus_test_password

Usage:
    PYTHONPATH=src python3.13 tests/benchmarks/bench_write_buffer_pg.py
"""

from __future__ import annotations

import statistics
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── Setup ────────────────────────────────────────────────────────────────

PG_URL = "postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test"

WRITE_COUNT = 200  # Number of writes per benchmark run
BATCH_SIZES = [1, 10, 50]  # Test different batch sizes


def make_engine(pool_size: int = 5, max_overflow: int = 10):
    return create_engine(
        PG_URL,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


@contextmanager
def pg_schema(engine):
    """Create tables, yield, then drop them (clean slate per benchmark)."""
    from nexus.storage.models._base import Base

    # Import all models so they register with Base.metadata
    from nexus.storage.models.file_path import FilePathModel  # noqa: F401
    from nexus.storage.models.operation_log import OperationLogModel  # noqa: F401
    from nexus.storage.models.version_history import VersionHistoryModel  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(engine)


def count_rows(session_factory, table: str) -> int:
    with session_factory() as session:
        return session.execute(text(f"SELECT count(*) FROM {table}")).scalar()


# ── Fake metadata object ────────────────────────────────────────────────


class FakeMetadata:
    """Minimal object satisfying FileMetadata protocol for benchmarks."""

    def __init__(self, path: str, content_hash: str = "abc123", size: int = 100):
        self.path = path
        self.backend_name = "local"
        self.physical_path = path
        self.size = size
        self.etag = content_hash
        self.content_hash = content_hash
        self.mime_type = "text/plain"
        self.created_at = None
        self.modified_at = None
        self.version = 1
        self.zone_id = "bench"
        self.created_by = None
        self.is_directory = False
        self.owner_id = None


# ── Benchmark: Synchronous RecordStoreSyncer ────────────────────────────


def bench_sync(engine, session_factory, n: int) -> dict:
    """Benchmark sync writes (one DB round-trip per write)."""
    from nexus.storage.record_store_syncer import RecordStoreSyncer

    syncer = RecordStoreSyncer(session_factory)

    latencies = []
    for i in range(n):
        meta = FakeMetadata(
            path=f"/bench/sync/file_{i}.txt", content_hash=f"hash_{i:06d}", size=i * 100
        )
        t0 = time.perf_counter()
        syncer.on_write(meta, is_new=True, path=meta.path, zone_id="bench")
        latencies.append(time.perf_counter() - t0)

    # Verify rows were written
    fp_count = count_rows(session_factory, "file_paths")
    op_count = count_rows(session_factory, "operation_log")
    vh_count = count_rows(session_factory, "version_history")

    return {
        "mode": "sync",
        "writes": n,
        "total_sec": sum(latencies),
        "mean_ms": statistics.mean(latencies) * 1000,
        "median_ms": statistics.median(latencies) * 1000,
        "p95_ms": sorted(latencies)[int(n * 0.95)] * 1000,
        "p99_ms": sorted(latencies)[int(n * 0.99)] * 1000,
        "throughput_wps": n / sum(latencies),
        "file_paths": fp_count,
        "operation_log": op_count,
        "version_history": vh_count,
    }


# ── Benchmark: Buffered WriteBuffer ─────────────────────────────────────


def bench_buffered(
    engine, session_factory, n: int, flush_interval_ms: int = 50, max_buffer_size: int = 50
) -> dict:
    """Benchmark buffered writes (hot path = enqueue only, flush in background)."""
    from nexus.storage.record_store_syncer import BufferedRecordStoreSyncer

    syncer = BufferedRecordStoreSyncer(
        session_factory,
        flush_interval_ms=flush_interval_ms,
        max_buffer_size=max_buffer_size,
    )
    syncer.start()

    latencies = []
    for i in range(n):
        meta = FakeMetadata(
            path=f"/bench/buffered/file_{i}.txt", content_hash=f"hash_{i:06d}", size=i * 100
        )
        t0 = time.perf_counter()
        syncer.on_write(meta, is_new=True, path=meta.path, zone_id="bench")
        latencies.append(time.perf_counter() - t0)

    # Wait for buffer to flush completely
    flush_start = time.perf_counter()
    syncer.stop(timeout=30.0)
    flush_wait = time.perf_counter() - flush_start

    # Verify rows were written
    fp_count = count_rows(session_factory, "file_paths")
    op_count = count_rows(session_factory, "operation_log")
    vh_count = count_rows(session_factory, "version_history")

    return {
        "mode": f"buffered(interval={flush_interval_ms}ms, batch={max_buffer_size})",
        "writes": n,
        "total_sec": sum(latencies),
        "mean_ms": statistics.mean(latencies) * 1000,
        "median_ms": statistics.median(latencies) * 1000,
        "p95_ms": sorted(latencies)[int(n * 0.95)] * 1000,
        "p99_ms": sorted(latencies)[int(n * 0.99)] * 1000,
        "throughput_wps": n / sum(latencies),
        "flush_wait_sec": flush_wait,
        "file_paths": fp_count,
        "operation_log": op_count,
        "version_history": vh_count,
    }


# ── Main ────────────────────────────────────────────────────────────────


def print_result(r: dict):
    print(f"\n  Mode:       {r['mode']}")
    print(f"  Writes:     {r['writes']}")
    print(f"  Total:      {r['total_sec']:.3f}s")
    print(f"  Mean:       {r['mean_ms']:.3f}ms")
    print(f"  Median:     {r['median_ms']:.3f}ms")
    print(f"  P95:        {r['p95_ms']:.3f}ms")
    print(f"  P99:        {r['p99_ms']:.3f}ms")
    print(f"  Throughput: {r['throughput_wps']:.0f} writes/sec")
    if "flush_wait_sec" in r:
        print(f"  Flush wait: {r['flush_wait_sec']:.3f}s")
    print(
        f"  DB rows:    file_paths={r['file_paths']}, operation_log={r['operation_log']}, version_history={r['version_history']}"
    )


def main():
    print("=" * 70)
    print("WriteBuffer PostgreSQL Benchmark (Issue #1246)")
    print("=" * 70)

    engine = make_engine()

    # Verify PostgreSQL connectivity
    with engine.connect() as conn:
        ver = conn.execute(text("SELECT version()")).scalar()
        print(f"\nPostgreSQL: {ver}")

    # ── Sync baseline ──
    print("\n" + "-" * 70)
    print(f"SYNC BASELINE ({WRITE_COUNT} writes)")
    print("-" * 70)

    with pg_schema(engine):
        sf = sessionmaker(bind=engine)
        sync_result = bench_sync(engine, sf, WRITE_COUNT)
        print_result(sync_result)

    # ── Buffered variants ──
    configs = [
        (50, 20),  # 50ms interval, batch of 20
        (50, 50),  # 50ms interval, batch of 50
        (100, 100),  # 100ms interval, batch of 100
    ]

    for interval, batch in configs:
        print("\n" + "-" * 70)
        print(f"BUFFERED (interval={interval}ms, batch={batch}, {WRITE_COUNT} writes)")
        print("-" * 70)

        with pg_schema(engine):
            sf = sessionmaker(bind=engine)
            buf_result = bench_buffered(
                engine, sf, WRITE_COUNT, flush_interval_ms=interval, max_buffer_size=batch
            )
            print_result(buf_result)

            # Speedup
            if sync_result["mean_ms"] > 0:
                speedup = sync_result["mean_ms"] / buf_result["mean_ms"]
                print(f"  Speedup:    {speedup:.1f}x faster hot path vs sync")

    # ── Summary ──
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Sync mean latency:     {sync_result['mean_ms']:.3f}ms/write")
    print(f"  Sync throughput:       {sync_result['throughput_wps']:.0f} writes/sec")
    print("\n  The WriteBuffer decouples the hot path (enqueue) from the")
    print("  cold path (flush to PG). For PostgreSQL with network latency,")
    print(f"  the hot path is ~0.01ms vs ~{sync_result['mean_ms']:.1f}ms for sync.")
    print("=" * 70)


if __name__ == "__main__":
    main()
