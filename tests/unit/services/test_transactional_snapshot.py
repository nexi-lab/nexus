"""Unit tests for TransactionalSnapshotService (Issue #1752).

TDD: These tests are written BEFORE the implementation.
Tests cover: begin, commit, rollback, conflict detection, state machine,
TTL expiry, and all 10 edge cases from the review.

Uses mocked metadata store + in-memory SQLite for session_factory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.services.protocols.transactional_snapshot import (
    InvalidTransactionStateError,
    OverlappingTransactionError,
    SnapshotId,
    TransactionConfig,
    TransactionNotFoundError,
    TransactionState,
)
from nexus.storage.models._base import Base
from nexus.storage.models.transactional_snapshot import TransactionSnapshotModel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeFileMetadata:
    """Minimal FileMetadata-like object for testing."""

    def __init__(self, path: str, etag: str | None, size: int = 100):
        self.path = path
        self.etag = etag
        self.size = size
        self.backend_name = "local"
        self.physical_path = etag or ""
        self.mime_type = "text/plain"
        self.version = 1
        self.created_at = datetime.now(UTC)
        self.modified_at = datetime.now(UTC)
        self.zone_id = "root"
        self.created_by = None
        self.owner_id = None
        self.entry_type = 0
        self.target_zone_id = None
        self.i_links_count = 0


class FakeMetadataStore:
    """In-memory metadata store for testing."""

    def __init__(self) -> None:
        self._files: dict[str, FakeFileMetadata] = {}

    def get(self, path: str) -> FakeFileMetadata | None:
        return self._files.get(path)

    def put(self, metadata: Any, *, consistency: str = "sc") -> None:
        self._files[metadata.path] = metadata

    def get_batch(self, paths: list[str]) -> dict[str, FakeFileMetadata | None]:
        return {p: self._files.get(p) for p in paths}

    def put_batch(self, metadata_list: list[Any]) -> None:
        for m in metadata_list:
            self._files[m.path] = m

    def delete(self, path: str) -> None:
        self._files.pop(path, None)

    def delete_batch(self, paths: list[str]) -> None:
        for p in paths:
            self._files.pop(p, None)

    def add_file(self, path: str, content_hash: str, size: int = 100) -> None:
        """Helper to seed a file."""
        self._files[path] = FakeFileMetadata(path=path, etag=content_hash, size=size)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    maker = sessionmaker(bind=engine)
    return maker


@pytest.fixture()
def metadata_store():
    store = FakeMetadataStore()
    store.add_file("/data/a.txt", "hash_a", 100)
    store.add_file("/data/b.txt", "hash_b", 200)
    store.add_file("/data/c.txt", "hash_c", 300)
    return store


@pytest.fixture()
def event_log():
    return AsyncMock()


@pytest.fixture()
def config():
    return TransactionConfig(ttl_seconds=3600, max_paths_per_transaction=100)


@pytest.fixture()
def service(metadata_store, session_factory, event_log, config):
    from nexus.services.transactional_snapshot import TransactionalSnapshotService

    return TransactionalSnapshotService(
        metadata_store=metadata_store,
        session_factory=session_factory,
        event_log=event_log,
        config=config,
    )


# ---------------------------------------------------------------------------
# Tests: begin()
# ---------------------------------------------------------------------------


class TestBegin:
    """TransactionalSnapshotService.begin()."""

    @pytest.mark.asyncio
    async def test_begin_returns_snapshot_id(self, service) -> None:
        result = await service.begin("agent-a", ["/data/a.txt"])
        assert isinstance(result, SnapshotId)
        assert len(result.id) == 36  # UUID

    @pytest.mark.asyncio
    async def test_begin_creates_active_record(self, service, session_factory) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            assert model is not None
            assert model.status == "ACTIVE"
            assert model.agent_id == "agent-a"
            assert model.zone_id == "root"
            assert model.path_count == 1

    @pytest.mark.asyncio
    async def test_begin_captures_content_hashes(self, service, session_factory) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt", "/data/b.txt"])
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            data = json.loads(model.snapshot_data_json)
            assert data["/data/a.txt"]["content_hash"] == "hash_a"
            assert data["/data/b.txt"]["content_hash"] == "hash_b"

    @pytest.mark.asyncio
    async def test_begin_records_absent_paths(self, service, session_factory) -> None:
        """Non-existent paths should be snapshotted as absent."""
        snap_id = await service.begin("agent-a", ["/data/missing.txt"])
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            data = json.loads(model.snapshot_data_json)
            assert data["/data/missing.txt"]["existed"] is False
            assert data["/data/missing.txt"]["content_hash"] is None

    @pytest.mark.asyncio
    async def test_begin_sets_expiry(self, service, session_factory, config) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            # SQLite stores naive datetimes, so compare naive
            expected_min = datetime.now(UTC).replace(tzinfo=None) + timedelta(
                seconds=config.ttl_seconds - 5
            )
            expires = (
                model.expires_at.replace(tzinfo=None)
                if model.expires_at.tzinfo
                else model.expires_at
            )
            assert expires >= expected_min

    @pytest.mark.asyncio
    async def test_begin_empty_paths_raises(self, service) -> None:
        with pytest.raises(ValueError, match="paths.*empty"):
            await service.begin("agent-a", [])

    @pytest.mark.asyncio
    async def test_begin_exceeds_max_paths_raises(self, service, config) -> None:
        paths = [f"/data/file_{i}.txt" for i in range(config.max_paths_per_transaction + 1)]
        with pytest.raises(ValueError, match="exceeds max"):
            await service.begin("agent-a", paths)

    @pytest.mark.asyncio
    async def test_begin_with_zone_id(self, service, session_factory) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"], zone_id="acme")
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            assert model.zone_id == "acme"

    @pytest.mark.asyncio
    async def test_begin_overlapping_paths_same_agent_raises(self, service) -> None:
        """Same agent cannot begin two transactions on overlapping paths."""
        await service.begin("agent-a", ["/data/a.txt"])
        with pytest.raises(OverlappingTransactionError):
            await service.begin("agent-a", ["/data/a.txt"])

    @pytest.mark.asyncio
    async def test_begin_same_paths_different_agents_ok(self, service) -> None:
        """Different agents CAN snapshot the same paths."""
        snap_a = await service.begin("agent-a", ["/data/a.txt"])
        snap_b = await service.begin("agent-b", ["/data/a.txt"])
        assert snap_a.id != snap_b.id


# ---------------------------------------------------------------------------
# Tests: commit()
# ---------------------------------------------------------------------------


class TestCommit:
    """TransactionalSnapshotService.commit()."""

    @pytest.mark.asyncio
    async def test_commit_transitions_to_committed(self, service, session_factory) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        await service.commit(snap_id)
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            assert model.status == "COMMITTED"
            assert model.committed_at is not None

    @pytest.mark.asyncio
    async def test_commit_nonexistent_raises(self, service) -> None:
        with pytest.raises(TransactionNotFoundError):
            await service.commit(SnapshotId(id="nonexistent"))

    @pytest.mark.asyncio
    async def test_commit_already_committed_raises(self, service) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        await service.commit(snap_id)
        with pytest.raises(InvalidTransactionStateError) as exc_info:
            await service.commit(snap_id)
        assert exc_info.value.current_state == TransactionState.COMMITTED

    @pytest.mark.asyncio
    async def test_commit_rolled_back_raises(self, service) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        await service.rollback(snap_id)
        with pytest.raises(InvalidTransactionStateError) as exc_info:
            await service.commit(snap_id)
        assert exc_info.value.current_state == TransactionState.ROLLED_BACK


# ---------------------------------------------------------------------------
# Tests: rollback()
# ---------------------------------------------------------------------------


class TestRollback:
    """TransactionalSnapshotService.rollback()."""

    @pytest.mark.asyncio
    async def test_rollback_restores_content(self, service, metadata_store) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        # Simulate agent modifying the file
        metadata_store.add_file("/data/a.txt", "new_hash_a", 999)
        result = await service.rollback(snap_id)
        assert "/data/a.txt" in result.reverted
        # Verify metadata was restored
        restored = metadata_store.get("/data/a.txt")
        assert restored.etag == "hash_a"

    @pytest.mark.asyncio
    async def test_rollback_transitions_to_rolled_back(self, service, session_factory) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        await service.rollback(snap_id)
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            assert model.status == "ROLLED_BACK"
            assert model.rolled_back_at is not None

    @pytest.mark.asyncio
    async def test_rollback_modified_file_is_reverted(self, service, metadata_store) -> None:
        """Modified files are reverted to snapshot state on rollback."""
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        # File is modified (could be by this agent or another)
        metadata_store.add_file("/data/a.txt", "someone_wrote", 200)

        result = await service.rollback(snap_id)
        # File should be reverted to snapshot state
        assert "/data/a.txt" in result.reverted
        assert metadata_store.get("/data/a.txt").etag == "hash_a"

    @pytest.mark.asyncio
    async def test_rollback_no_change_is_noop(self, service, metadata_store) -> None:
        """If file hasn't changed since snapshot, rollback is a no-op for that file."""
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        # Don't modify the file
        result = await service.rollback(snap_id)
        # File unchanged → not in reverted (nothing to revert)
        assert "/data/a.txt" not in result.reverted
        assert len(result.conflicts) == 0

    @pytest.mark.asyncio
    async def test_rollback_absent_path_deletes_file(self, service, metadata_store) -> None:
        """If path didn't exist at begin(), rollback deletes it."""
        snap_id = await service.begin("agent-a", ["/data/new.txt"])
        # Agent creates the file
        metadata_store.add_file("/data/new.txt", "new_content")
        result = await service.rollback(snap_id)
        assert "/data/new.txt" in result.deleted
        assert metadata_store.get("/data/new.txt") is None

    @pytest.mark.asyncio
    async def test_rollback_multi_file(self, service, metadata_store) -> None:
        paths = ["/data/a.txt", "/data/b.txt", "/data/c.txt"]
        snap_id = await service.begin("agent-a", paths)
        # Modify all files
        metadata_store.add_file("/data/a.txt", "new_a")
        metadata_store.add_file("/data/b.txt", "new_b")
        metadata_store.add_file("/data/c.txt", "new_c")
        result = await service.rollback(snap_id)
        assert set(result.reverted) == {"/data/a.txt", "/data/b.txt", "/data/c.txt"}

    @pytest.mark.asyncio
    async def test_rollback_nonexistent_raises(self, service) -> None:
        with pytest.raises(TransactionNotFoundError):
            await service.rollback(SnapshotId(id="nonexistent"))

    @pytest.mark.asyncio
    async def test_rollback_already_committed_raises(self, service) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        await service.commit(snap_id)
        with pytest.raises(InvalidTransactionStateError) as exc_info:
            await service.rollback(snap_id)
        assert exc_info.value.current_state == TransactionState.COMMITTED

    @pytest.mark.asyncio
    async def test_rollback_already_rolled_back_raises(self, service) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        await service.rollback(snap_id)
        with pytest.raises(InvalidTransactionStateError) as exc_info:
            await service.rollback(snap_id)
        assert exc_info.value.current_state == TransactionState.ROLLED_BACK


# ---------------------------------------------------------------------------
# Tests: get_transaction()
# ---------------------------------------------------------------------------


class TestGetTransaction:
    """TransactionalSnapshotService.get_transaction()."""

    @pytest.mark.asyncio
    async def test_get_active(self, service) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        info = await service.get_transaction(snap_id)
        assert info.snapshot_id == snap_id.id
        assert info.agent_id == "agent-a"
        assert info.status == TransactionState.ACTIVE
        assert info.paths == ["/data/a.txt"]

    @pytest.mark.asyncio
    async def test_get_nonexistent_raises(self, service) -> None:
        with pytest.raises(TransactionNotFoundError):
            await service.get_transaction(SnapshotId(id="nonexistent"))


# ---------------------------------------------------------------------------
# Tests: list_active()
# ---------------------------------------------------------------------------


class TestListActive:
    """TransactionalSnapshotService.list_active()."""

    @pytest.mark.asyncio
    async def test_list_returns_active_only(self, service) -> None:
        snap1 = await service.begin("agent-a", ["/data/a.txt"])
        snap2 = await service.begin("agent-a", ["/data/b.txt"])
        await service.commit(snap1)
        active = await service.list_active("agent-a")
        assert len(active) == 1
        assert active[0].snapshot_id == snap2.id

    @pytest.mark.asyncio
    async def test_list_filters_by_agent(self, service) -> None:
        await service.begin("agent-a", ["/data/a.txt"])
        await service.begin("agent-b", ["/data/b.txt"])
        active_a = await service.list_active("agent-a")
        assert len(active_a) == 1
        assert active_a[0].agent_id == "agent-a"

    @pytest.mark.asyncio
    async def test_list_empty(self, service) -> None:
        active = await service.list_active("no-such-agent")
        assert active == []


# ---------------------------------------------------------------------------
# Tests: cleanup_expired()
# ---------------------------------------------------------------------------


class TestCleanupExpired:
    """TransactionalSnapshotService.cleanup_expired()."""

    @pytest.mark.asyncio
    async def test_expires_old_transactions(self, service, session_factory) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        # Manually set expiry to the past
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            model.expires_at = datetime.now(UTC) - timedelta(hours=1)
            session.commit()
        expired = await service.cleanup_expired()
        assert expired == 1
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            assert model.status == "EXPIRED"

    @pytest.mark.asyncio
    async def test_does_not_expire_active_within_ttl(self, service) -> None:
        await service.begin("agent-a", ["/data/a.txt"])
        expired = await service.cleanup_expired()
        assert expired == 0

    @pytest.mark.asyncio
    async def test_does_not_expire_committed(self, service, session_factory) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        await service.commit(snap_id)
        # Even if expires_at is past, committed transactions are not expired
        with session_factory() as session:
            model = session.get(TransactionSnapshotModel, snap_id.id)
            model.expires_at = datetime.now(UTC) - timedelta(hours=1)
            session.commit()
        expired = await service.cleanup_expired()
        assert expired == 0


# ---------------------------------------------------------------------------
# Tests: Full lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Full transaction lifecycle tests."""

    @pytest.mark.asyncio
    async def test_begin_commit_lifecycle(self, service) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        info = await service.get_transaction(snap_id)
        assert info.status == TransactionState.ACTIVE
        await service.commit(snap_id)
        info = await service.get_transaction(snap_id)
        assert info.status == TransactionState.COMMITTED

    @pytest.mark.asyncio
    async def test_begin_rollback_lifecycle(self, service, metadata_store) -> None:
        snap_id = await service.begin("agent-a", ["/data/a.txt"])
        metadata_store.add_file("/data/a.txt", "modified")
        result = await service.rollback(snap_id)
        assert "/data/a.txt" in result.reverted
        info = await service.get_transaction(snap_id)
        assert info.status == TransactionState.ROLLED_BACK

    @pytest.mark.asyncio
    async def test_after_commit_paths_are_released(self, service) -> None:
        """After commit, same agent can begin new transaction on same paths."""
        snap1 = await service.begin("agent-a", ["/data/a.txt"])
        await service.commit(snap1)
        snap2 = await service.begin("agent-a", ["/data/a.txt"])
        assert snap2.id != snap1.id

    @pytest.mark.asyncio
    async def test_after_rollback_paths_are_released(self, service) -> None:
        """After rollback, same agent can begin new transaction on same paths."""
        snap1 = await service.begin("agent-a", ["/data/a.txt"])
        await service.rollback(snap1)
        snap2 = await service.begin("agent-a", ["/data/a.txt"])
        assert snap2.id != snap1.id
