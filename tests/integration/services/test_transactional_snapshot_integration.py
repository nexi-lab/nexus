"""Integration tests for TransactionalSnapshotService (Issue #1752).

Uses real SQLite + real in-memory metadata store to verify:
- Multi-file atomic rollback
- Concurrent agent isolation
- Conflict detection across agents
- Large transaction batch performance
- Absent path handling
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.metadata import FileMetadata
from nexus.services.protocols.transactional_snapshot import (
    OverlappingTransactionError,
    TransactionConfig,
    TransactionState,
)
from nexus.services.transactional_snapshot import TransactionalSnapshotService
from nexus.storage.models._base import Base

# ---------------------------------------------------------------------------
# In-memory metadata store (dict-backed, implements batch APIs)
# ---------------------------------------------------------------------------


class InMemoryMetadataStore:
    """Dict-backed FileMetadataProtocol for integration tests."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, meta: FileMetadata) -> None:
        self._store[meta.path] = meta

    def delete(self, path: str) -> None:
        self._store.pop(path, None)

    def get_batch(self, paths: list[str] | Any) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def put_batch(self, metadata_list: list[FileMetadata] | Any) -> None:
        for meta in metadata_list:
            self._store[meta.path] = meta

    def delete_batch(self, paths: list[str] | Any) -> None:
        for p in paths:
            self._store.pop(p, None)

    def list_all(self) -> dict[str, FileMetadata]:
        return dict(self._store)


def _make_file(
    path: str,
    content_hash: str = "hash-default",
    size: int = 100,
) -> FileMetadata:
    """Create a FileMetadata with the given attributes."""
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=content_hash,
        size=size,
        etag=content_hash,
        modified_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture()
def engine(temp_dir: Path):
    """SQLite engine with all tables created."""
    db_path = temp_dir / "integration_test.db"
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    """SQLAlchemy session factory."""
    return sessionmaker(bind=engine)


@pytest.fixture()
def metadata_store() -> InMemoryMetadataStore:
    return InMemoryMetadataStore()


@pytest.fixture()
def service(metadata_store, session_factory) -> TransactionalSnapshotService:
    return TransactionalSnapshotService(
        metadata_store=metadata_store,
        session_factory=session_factory,
    )


@pytest.fixture()
def small_config_service(metadata_store, session_factory) -> TransactionalSnapshotService:
    """Service with small TTL and path limits for testing."""
    return TransactionalSnapshotService(
        metadata_store=metadata_store,
        session_factory=session_factory,
        config=TransactionConfig(ttl_seconds=1, max_paths_per_transaction=10),
    )


# ---------------------------------------------------------------------------
# TestAtomicRollback
# ---------------------------------------------------------------------------


class TestAtomicRollback:
    """Multi-file rollback atomicity with real SQLite."""

    @pytest.mark.asyncio
    async def test_rollback_restores_all_files(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Rollback of 5 files restores all to snapshot state."""
        paths = [f"/data/file_{i}.txt" for i in range(5)]
        original_hashes = [f"original-{i}" for i in range(5)]
        for path, h in zip(paths, original_hashes):
            metadata_store.put(_make_file(path, content_hash=h))

        sid = await service.begin("agent-a", paths)

        # Simulate agent modifications
        for i, path in enumerate(paths):
            metadata_store.put(_make_file(path, content_hash=f"modified-{i}"))

        result = await service.rollback(sid)

        assert len(result.reverted) == 5
        for i, path in enumerate(paths):
            restored = metadata_store.get(path)
            assert restored is not None
            assert restored.etag == f"original-{i}"

    @pytest.mark.asyncio
    async def test_rollback_with_mixed_changes(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Rollback handles mix of modified, deleted, and unchanged files."""
        metadata_store.put(_make_file("/a.txt", "hash-a"))
        metadata_store.put(_make_file("/b.txt", "hash-b"))
        metadata_store.put(_make_file("/c.txt", "hash-c"))

        sid = await service.begin("agent-a", ["/a.txt", "/b.txt", "/c.txt"])

        # /a.txt modified, /b.txt deleted, /c.txt unchanged
        metadata_store.put(_make_file("/a.txt", "hash-a-modified"))
        metadata_store.delete("/b.txt")

        result = await service.rollback(sid)

        assert "/a.txt" in result.reverted
        assert "/b.txt" in result.reverted
        assert "/c.txt" not in result.reverted
        assert metadata_store.get("/a.txt").etag == "hash-a"
        assert metadata_store.get("/b.txt").etag == "hash-b"
        assert metadata_store.get("/c.txt").etag == "hash-c"


# ---------------------------------------------------------------------------
# TestConcurrentAgents
# ---------------------------------------------------------------------------


class TestConcurrentAgents:
    """Multiple agents operating on separate paths in parallel."""

    @pytest.mark.asyncio
    async def test_two_agents_non_overlapping_paths(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Two agents can have concurrent transactions on disjoint paths."""
        metadata_store.put(_make_file("/agent-a/data.txt", "a-hash"))
        metadata_store.put(_make_file("/agent-b/data.txt", "b-hash"))

        sid_a = await service.begin("agent-a", ["/agent-a/data.txt"])
        sid_b = await service.begin("agent-b", ["/agent-b/data.txt"])

        # Both agents modify their files
        metadata_store.put(_make_file("/agent-a/data.txt", "a-modified"))
        metadata_store.put(_make_file("/agent-b/data.txt", "b-modified"))

        # Agent A commits, Agent B rollbacks
        await service.commit(sid_a)
        result_b = await service.rollback(sid_b)

        assert metadata_store.get("/agent-a/data.txt").etag == "a-modified"
        assert metadata_store.get("/agent-b/data.txt").etag == "b-hash"
        assert len(result_b.reverted) == 1

    @pytest.mark.asyncio
    async def test_overlapping_paths_same_agent_rejected(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Same agent cannot have overlapping paths across active transactions."""
        metadata_store.put(_make_file("/shared.txt", "hash"))

        await service.begin("agent-a", ["/shared.txt"])

        with pytest.raises(OverlappingTransactionError, match="agent-a"):
            await service.begin("agent-a", ["/shared.txt"])

    @pytest.mark.asyncio
    async def test_same_paths_different_agents_ok(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Different agents can snapshot the same path (no cross-agent overlap check)."""
        metadata_store.put(_make_file("/shared.txt", "hash"))

        sid_a = await service.begin("agent-a", ["/shared.txt"])
        sid_b = await service.begin("agent-b", ["/shared.txt"])

        # Both succeed
        assert sid_a.id != sid_b.id


# ---------------------------------------------------------------------------
# TestConflictDetection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    """Agent A snapshots, Agent B writes, Agent A rollbacks."""

    @pytest.mark.asyncio
    async def test_agent_a_rollback_reverts_agent_b_change(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """When Agent A rollbacks, file is restored even if Agent B modified it.

        Optimistic concurrency: rollback always restores to snapshot state.
        """
        metadata_store.put(_make_file("/shared.txt", "original"))

        sid_a = await service.begin("agent-a", ["/shared.txt"])

        # Agent B modifies the file (not tracked by agent-a's transaction)
        metadata_store.put(_make_file("/shared.txt", "agent-b-wrote-this"))

        result = await service.rollback(sid_a)

        assert "/shared.txt" in result.reverted
        assert metadata_store.get("/shared.txt").etag == "original"


# ---------------------------------------------------------------------------
# TestAbsentPaths
# ---------------------------------------------------------------------------


class TestAbsentPaths:
    """Snapshot absent paths, rollback deletes files that didn't exist."""

    @pytest.mark.asyncio
    async def test_snapshot_absent_then_file_created_then_rollback_deletes(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Path didn't exist at snapshot time. Agent creates it. Rollback deletes it."""
        sid = await service.begin("agent-a", ["/new-file.txt"])

        # Agent creates the file
        metadata_store.put(_make_file("/new-file.txt", "new-hash"))

        result = await service.rollback(sid)

        assert "/new-file.txt" in result.deleted
        assert metadata_store.get("/new-file.txt") is None

    @pytest.mark.asyncio
    async def test_snapshot_absent_stays_absent_noop(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Path didn't exist and still doesn't — rollback is a no-op."""
        sid = await service.begin("agent-a", ["/ghost.txt"])

        result = await service.rollback(sid)

        assert result.reverted == []
        assert result.deleted == []


# ---------------------------------------------------------------------------
# TestLargeTransactions
# ---------------------------------------------------------------------------


class TestLargeTransactions:
    """Batch performance with 100+ files."""

    @pytest.mark.asyncio
    async def test_100_files_roundtrip(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Snapshot and rollback 100 files uses batch APIs efficiently."""
        paths = [f"/data/file_{i:03d}.txt" for i in range(100)]
        for i, path in enumerate(paths):
            metadata_store.put(_make_file(path, content_hash=f"original-{i}"))

        sid = await service.begin("agent-a", paths)

        # Modify all
        for i, path in enumerate(paths):
            metadata_store.put(_make_file(path, content_hash=f"modified-{i}"))

        result = await service.rollback(sid)

        assert len(result.reverted) == 100
        assert result.stats["paths_total"] == 100
        assert result.stats["paths_reverted"] == 100

        # Verify all restored
        for i, path in enumerate(paths):
            assert metadata_store.get(path).etag == f"original-{i}"


# ---------------------------------------------------------------------------
# TestTransactionStateTracking
# ---------------------------------------------------------------------------


class TestTransactionStateTracking:
    """Verify state tracking through real DB queries."""

    @pytest.mark.asyncio
    async def test_list_active_reflects_state(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """list_active returns correct snapshot after begin/commit/rollback."""
        metadata_store.put(_make_file("/a.txt"))
        metadata_store.put(_make_file("/b.txt"))

        sid1 = await service.begin("agent-a", ["/a.txt"])
        sid2 = await service.begin("agent-a", ["/b.txt"])

        active = await service.list_active("agent-a")
        assert len(active) == 2

        await service.commit(sid1)
        active = await service.list_active("agent-a")
        assert len(active) == 1
        assert active[0].snapshot_id == sid2.id

    @pytest.mark.asyncio
    async def test_get_transaction_after_rollback(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Transaction info shows ROLLED_BACK state after rollback."""
        metadata_store.put(_make_file("/data.txt"))

        sid = await service.begin("agent-a", ["/data.txt"])
        await service.rollback(sid)

        info = await service.get_transaction(sid)
        assert info.status == TransactionState.ROLLED_BACK
        assert info.rolled_back_at is not None


# ---------------------------------------------------------------------------
# TestFullLifecycleIntegration
# ---------------------------------------------------------------------------


class TestFullLifecycleIntegration:
    """End-to-end lifecycle with real DB and metadata store."""

    @pytest.mark.asyncio
    async def test_begin_modify_rollback_verify(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Full cycle: begin -> agent modifies -> rollback -> original restored."""
        metadata_store.put(_make_file("/config.json", "config-v1", size=2048))

        sid = await service.begin("agent-a", ["/config.json"])

        # Agent makes changes
        metadata_store.put(_make_file("/config.json", "config-v2", size=4096))
        assert metadata_store.get("/config.json").etag == "config-v2"

        result = await service.rollback(sid)

        assert "/config.json" in result.reverted
        restored = metadata_store.get("/config.json")
        assert restored.etag == "config-v1"
        assert restored.size == 2048

    @pytest.mark.asyncio
    async def test_begin_modify_commit_keeps_changes(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore
    ) -> None:
        """Commit discards snapshot — agent's changes are permanent."""
        metadata_store.put(_make_file("/data.txt", "v1"))

        sid = await service.begin("agent-a", ["/data.txt"])
        metadata_store.put(_make_file("/data.txt", "v2"))

        await service.commit(sid)

        assert metadata_store.get("/data.txt").etag == "v2"

    @pytest.mark.asyncio
    async def test_cleanup_expired_with_real_db(
        self, service: TransactionalSnapshotService, metadata_store: InMemoryMetadataStore, session_factory
    ) -> None:
        """Expired transactions are cleaned up from real SQLite DB."""
        from nexus.storage.models.transactional_snapshot import TransactionSnapshotModel

        metadata_store.put(_make_file("/data.txt"))
        sid = await service.begin("agent-a", ["/data.txt"])

        # Manually expire the transaction
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, sid.id)
            model.expires_at = datetime(2020, 1, 1)
            session.commit()

        count = await service.cleanup_expired()
        assert count == 1

        info = await service.get_transaction(sid)
        assert info.status == TransactionState.EXPIRED
