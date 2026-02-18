"""Transactional Snapshot benchmarks (Issue #1752).

Performance benchmarks for snapshot begin/commit/rollback operations.
Targets: <20% overhead for 100 files, <50ms for individual operations.

Run with: uv run pytest tests/benchmarks/test_snapshot_benchmarks.py -v --override-ini="addopts="
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.metadata import FileMetadata
from nexus.services.transactional_snapshot import TransactionalSnapshotService
from nexus.storage.models._base import Base
from nexus.storage.models.transactional_snapshot import TransactionSnapshotModel  # noqa: F401

# ---------------------------------------------------------------------------
# In-memory metadata store for benchmarks
# ---------------------------------------------------------------------------


class BenchmarkMetadataStore:
    """Dict-backed metadata store for benchmarks."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, meta: FileMetadata) -> None:
        self._store[meta.path] = meta

    def get_batch(self, paths: list[str]) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def put_batch(self, metadata_list: list[FileMetadata]) -> None:
        for meta in metadata_list:
            self._store[meta.path] = meta

    def delete_batch(self, paths: list[str]) -> None:
        for p in paths:
            self._store.pop(p, None)


def _make_file(path: str, content_hash: str = "hash-default") -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=content_hash,
        size=100,
        etag=content_hash,
        modified_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bench_service():
    """Create a fresh service with in-memory SQLite for each benchmark."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    store = BenchmarkMetadataStore()
    svc = TransactionalSnapshotService(
        metadata_store=store,
        session_factory=session_factory,
    )
    return svc, store


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestSnapshotBeginPerformance:
    """Benchmark begin() at various file counts."""

    def test_begin_10_files(self, bench_service):
        svc, store = bench_service
        for i in range(10):
            store.put(_make_file(f"/file-{i}.txt", f"hash-{i}"))
        paths = [f"/file-{i}.txt" for i in range(10)]

        start = time.perf_counter()
        sid = asyncio.get_event_loop().run_until_complete(
            svc.begin(agent_id="bench-agent", paths=paths)
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert sid.id is not None
        assert elapsed_ms < 200, f"begin(10 files) took {elapsed_ms:.2f}ms (>200ms)"

    def test_begin_100_files(self, bench_service):
        svc, store = bench_service
        for i in range(100):
            store.put(_make_file(f"/file-{i}.txt", f"hash-{i}"))
        paths = [f"/file-{i}.txt" for i in range(100)]

        start = time.perf_counter()
        sid = asyncio.get_event_loop().run_until_complete(
            svc.begin(agent_id="bench-agent", paths=paths)
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert sid.id is not None
        assert elapsed_ms < 100, f"begin(100 files) took {elapsed_ms:.2f}ms (>100ms)"

    def test_begin_500_files(self, bench_service):
        svc, store = bench_service
        for i in range(500):
            store.put(_make_file(f"/file-{i}.txt", f"hash-{i}"))
        paths = [f"/file-{i}.txt" for i in range(500)]

        start = time.perf_counter()
        sid = asyncio.get_event_loop().run_until_complete(
            svc.begin(agent_id="bench-agent", paths=paths)
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert sid.id is not None
        assert elapsed_ms < 200, f"begin(500 files) took {elapsed_ms:.2f}ms (>200ms)"


@pytest.mark.benchmark
class TestSnapshotCommitPerformance:
    """Benchmark commit() operation."""

    def test_commit_after_begin(self, bench_service):
        svc, store = bench_service
        for i in range(100):
            store.put(_make_file(f"/file-{i}.txt", f"hash-{i}"))
        paths = [f"/file-{i}.txt" for i in range(100)]

        loop = asyncio.get_event_loop()
        sid = loop.run_until_complete(svc.begin(agent_id="bench-agent", paths=paths))

        start = time.perf_counter()
        loop.run_until_complete(svc.commit(sid))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 20, f"commit() took {elapsed_ms:.2f}ms (>20ms)"


@pytest.mark.benchmark
class TestSnapshotRollbackPerformance:
    """Benchmark rollback() at various file counts."""

    def test_rollback_10_files(self, bench_service):
        svc, store = bench_service
        for i in range(10):
            store.put(_make_file(f"/file-{i}.txt", f"original-{i}"))
        paths = [f"/file-{i}.txt" for i in range(10)]

        loop = asyncio.get_event_loop()
        sid = loop.run_until_complete(svc.begin(agent_id="bench-agent", paths=paths))

        # Modify all files
        for i in range(10):
            store.put(_make_file(f"/file-{i}.txt", f"modified-{i}"))

        start = time.perf_counter()
        result = loop.run_until_complete(svc.rollback(sid))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(result.reverted) == 10
        assert elapsed_ms < 50, f"rollback(10 files) took {elapsed_ms:.2f}ms (>50ms)"

    def test_rollback_100_files(self, bench_service):
        svc, store = bench_service
        for i in range(100):
            store.put(_make_file(f"/file-{i}.txt", f"original-{i}"))
        paths = [f"/file-{i}.txt" for i in range(100)]

        loop = asyncio.get_event_loop()
        sid = loop.run_until_complete(svc.begin(agent_id="bench-agent", paths=paths))

        # Modify all files
        for i in range(100):
            store.put(_make_file(f"/file-{i}.txt", f"modified-{i}"))

        start = time.perf_counter()
        result = loop.run_until_complete(svc.rollback(sid))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(result.reverted) == 100
        assert elapsed_ms < 100, f"rollback(100 files) took {elapsed_ms:.2f}ms (>100ms)"


@pytest.mark.benchmark
class TestSnapshotOverhead:
    """Measure snapshot overhead relative to baseline write."""

    def test_full_cycle_100_files_under_budget(self, bench_service):
        """Full begin+modify+rollback for 100 files stays under 200ms."""
        svc, store = bench_service
        for i in range(100):
            store.put(_make_file(f"/file-{i}.txt", f"original-{i}"))
        paths = [f"/file-{i}.txt" for i in range(100)]

        loop = asyncio.get_event_loop()
        start = time.perf_counter()
        sid = loop.run_until_complete(svc.begin(agent_id="bench-agent", paths=paths))
        for i in range(100):
            store.put(_make_file(f"/file-{i}.txt", f"modified-{i}"))
        loop.run_until_complete(svc.rollback(sid))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 200, f"Full snapshot cycle (100 files) took {elapsed_ms:.2f}ms (>200ms)"
