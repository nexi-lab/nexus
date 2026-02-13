"""Benchmarks for WALEventLog (Rust-backed event log).

Run with: pytest tests/benchmarks/bench_event_log_wal.py --benchmark-json=benchmark.json -v
Track-only: no hard CI assertions (Decision #12).

Issue #1397
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.event_log import EventLogConfig

try:
    from nexus.services.event_log.wal_backend import WALEventLog, is_available

    if not is_available():
        pytest.skip("_nexus_wal extension not available", allow_module_level=True)
except ImportError:
    pytest.skip("_nexus_wal extension not available", allow_module_level=True)

pytest.importorskip("pytest_benchmark")


def _event(i: int = 0) -> FileEvent:
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path=f"/bench/file-{i}.txt",
        zone_id="zone-bench",
    )


@pytest.fixture()
def wal(tmp_path: Path) -> WALEventLog:
    config = EventLogConfig(wal_dir=tmp_path / "wal", sync_mode="none")
    log = WALEventLog(config)
    yield log  # type: ignore[misc]
    try:
        log._wal.close()
    except Exception:
        pass


def test_bench_append_single(benchmark: Any, wal: WALEventLog) -> None:
    """Benchmark single event append (no fsync)."""
    loop = asyncio.new_event_loop()

    def run() -> None:
        loop.run_until_complete(wal.append(_event()))

    benchmark(run)
    loop.close()


def test_bench_append_batch_1k(benchmark: Any, wal: WALEventLog) -> None:
    """Benchmark batch of 1000 events."""
    loop = asyncio.new_event_loop()
    events = [_event(i) for i in range(1000)]

    def run() -> None:
        loop.run_until_complete(wal.append_batch(events))

    benchmark(run)
    loop.close()


def test_bench_read_1k(benchmark: Any, wal: WALEventLog) -> None:
    """Benchmark reading 1000 events from middle of WAL."""
    loop = asyncio.new_event_loop()

    # Pre-populate
    events = [_event(i) for i in range(10_000)]
    loop.run_until_complete(wal.append_batch(events))

    def run() -> None:
        loop.run_until_complete(wal.read_from(5000, limit=1000))

    benchmark(run)
    loop.close()
