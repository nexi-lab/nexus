"""Unit tests for TransactionalSnapshotService (Issue #2131, Phase 6.1).

Tests the snapshot service's core lifecycle: begin, track, commit, rollback,
and cleanup. Uses mocks for CAS store, metadata store, and session factory.
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.snapshot.errors import (
    TransactionConflictError,
    TransactionNotActiveError,
)
from nexus.bricks.snapshot.service import TransactionalSnapshotService
from nexus.contracts.constants import ROOT_ZONE_ID

# ---------------------------------------------------------------------------
# Lightweight fakes (avoid heavy ORM dependency)
# ---------------------------------------------------------------------------


@dataclass
class FakeTransactionModel:
    """Minimal stand-in for TransactionSnapshotModel."""

    transaction_id: str = ""
    zone_id: str = ROOT_ZONE_ID
    agent_id: str | None = None
    status: str = "active"
    description: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(hours=1))
    completed_at: datetime | None = None
    entry_count: int = 0


class FakeMetadata:
    """Minimal metadata stand-in with etag attribute."""

    def __init__(self, etag: str | None = None) -> None:
        self.etag = etag


class FakeCASStore:
    """Fake CAS store that tracks hold/release calls."""

    def __init__(self) -> None:
        self.held: dict[str, int] = {}
        self.hold_calls: list[str] = []
        self.release_calls: list[str] = []

    def hold_reference(self, hash_: str) -> bool:
        self.hold_calls.append(hash_)
        self.held[hash_] = self.held.get(hash_, 0) + 1
        return True

    def release(self, hash_: str) -> None:
        self.release_calls.append(hash_)
        if hash_ in self.held:
            self.held[hash_] -= 1
            if self.held[hash_] <= 0:
                del self.held[hash_]


class FakeMetadataStore:
    """Fake metadata store backed by a dict."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, path: str) -> Any:
        return self.data.get(path)

    def put(self, meta: Any) -> None:
        self.data[meta.path] = meta

    def delete(self, path: str) -> None:
        self.data.pop(path, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cas_store() -> FakeCASStore:
    return FakeCASStore()


@pytest.fixture()
def metadata_store() -> FakeMetadataStore:
    return FakeMetadataStore()


@pytest.fixture()
def session_factory() -> Any:
    @contextmanager
    def _factory() -> Any:
        yield MagicMock()

    return _factory


def _fake_metadata_factory(**kwargs: Any) -> Any:
    """Fake metadata factory that returns a simple object with the given attrs."""
    obj = type("FakeFileMetadata", (), kwargs)()
    obj.path = kwargs.get("path", "")
    obj.etag = kwargs.get("etag", "")
    return obj


@pytest.fixture()
def service(
    session_factory: Any,
    cas_store: FakeCASStore,
    metadata_store: FakeMetadataStore,
) -> TransactionalSnapshotService:
    mock_record_store = MagicMock(session_factory=session_factory)
    svc = TransactionalSnapshotService(
        record_store=mock_record_store,
        cas_store=cas_store,
        metadata_store=metadata_store,
        metadata_factory=_fake_metadata_factory,
    )
    # Mock _persist_entry to avoid ORM model imports
    svc._persist_entry = MagicMock()  # type: ignore[method-assign]
    return svc


# ---------------------------------------------------------------------------
# Tests: track_write / track_delete (sync hot path)
# ---------------------------------------------------------------------------


class TestTrackWrite:
    """Tests for the synchronous track_write method."""

    def test_track_write_holds_cas_reference(
        self, service: TransactionalSnapshotService, cas_store: FakeCASStore
    ) -> None:
        """Tracking a write holds a CAS reference on the original hash."""
        service.registry.register("txn-1")
        service.track_write("txn-1", "/a.txt", "hash-orig", {"size": 10}, "hash-new")

        assert "hash-orig" in cas_store.hold_calls
        assert cas_store.held.get("hash-orig", 0) == 1

    def test_track_write_no_original_hash(
        self, service: TransactionalSnapshotService, cas_store: FakeCASStore
    ) -> None:
        """Tracking a write for a new file (no original hash) skips CAS hold."""
        service.registry.register("txn-1")
        service.track_write("txn-1", "/new.txt", None, None, "hash-new")

        assert len(cas_store.hold_calls) == 0

    def test_track_delete_holds_cas_reference(
        self, service: TransactionalSnapshotService, cas_store: FakeCASStore
    ) -> None:
        """Tracking a delete holds a CAS reference on the original hash."""
        service.registry.register("txn-1")
        service.track_delete("txn-1", "/b.txt", "hash-del", {"size": 5})

        assert "hash-del" in cas_store.hold_calls

    def test_is_tracked_returns_txn_id(self, service: TransactionalSnapshotService) -> None:
        """is_tracked returns the transaction ID for a tracked path."""
        service.registry.register("txn-1")
        service.track_write("txn-1", "/c.txt", "h1", None, "h2")

        assert service.is_tracked("/c.txt") == "txn-1"

    def test_is_tracked_returns_none_when_no_txn(
        self,
        service: TransactionalSnapshotService,
    ) -> None:
        """is_tracked returns None when no active transactions exist."""
        assert service.is_tracked("/anything.txt") is None


# ---------------------------------------------------------------------------
# Tests: commit (async)
# ---------------------------------------------------------------------------


class TestCommit:
    """Tests for commit with conflict detection."""

    @pytest.mark.asyncio
    async def test_commit_no_conflict(
        self,
        service: TransactionalSnapshotService,
        cas_store: FakeCASStore,
        metadata_store: FakeMetadataStore,
    ) -> None:
        """Commit succeeds when no conflicts exist, releases CAS holds."""
        metadata_store.data["/f.txt"] = FakeMetadata(etag="hash-new")

        txn = FakeTransactionModel(transaction_id="txn-c1", status="active", entry_count=1)

        from nexus.contracts.protocols.snapshot import SnapshotEntry

        fake_entry = SnapshotEntry(
            entry_id="e1",
            transaction_id="txn-c1",
            path="/f.txt",
            operation="write",
            original_hash="hash-orig",
            original_metadata=None,
            new_hash="hash-new",
            created_at=datetime.now(UTC),
        )

        service.registry.register("txn-c1")

        async def fake_to_thread(fn: Any, *args: Any) -> Any:
            return fn(*args)

        with (
            patch.object(
                service, "_load_transaction_with_entries", return_value=(txn, [fake_entry])
            ),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            result = await service.commit("txn-c1")

        assert result.status == "committed"
        assert "hash-orig" in cas_store.release_calls

    @pytest.mark.asyncio
    async def test_commit_with_conflict(
        self,
        service: TransactionalSnapshotService,
        metadata_store: FakeMetadataStore,
    ) -> None:
        """Commit raises TransactionConflictError when file was modified externally."""
        metadata_store.data["/f.txt"] = FakeMetadata(etag="someone-elses-hash")

        txn = FakeTransactionModel(transaction_id="txn-c2", status="active")
        from nexus.contracts.protocols.snapshot import SnapshotEntry

        fake_entry = SnapshotEntry(
            entry_id="e2",
            transaction_id="txn-c2",
            path="/f.txt",
            operation="write",
            original_hash="orig",
            original_metadata=None,
            new_hash="hash-expected",
            created_at=datetime.now(UTC),
        )

        service.registry.register("txn-c2")
        with (
            patch.object(
                service, "_load_transaction_with_entries", return_value=(txn, [fake_entry])
            ),
            pytest.raises(TransactionConflictError) as exc_info,
        ):
            await service.commit("txn-c2")

        assert len(exc_info.value.conflicts) == 1
        assert exc_info.value.conflicts[0].path == "/f.txt"

    @pytest.mark.asyncio
    async def test_commit_not_active_raises(
        self,
        service: TransactionalSnapshotService,
    ) -> None:
        """Commit on a non-active transaction raises TransactionNotActiveError."""
        txn = FakeTransactionModel(transaction_id="txn-c3", status="committed")

        with (
            patch.object(service, "_load_transaction_with_entries", return_value=(txn, [])),
            pytest.raises(TransactionNotActiveError) as exc_info,
        ):
            await service.commit("txn-c3")

        assert exc_info.value.transaction_id == "txn-c3"
        assert exc_info.value.status == "committed"


# ---------------------------------------------------------------------------
# Tests: rollback (async)
# ---------------------------------------------------------------------------


class TestRollback:
    """Tests for rollback restoring files to pre-transaction state."""

    @pytest.mark.asyncio
    async def test_rollback_write_restores_metadata(
        self,
        service: TransactionalSnapshotService,
        metadata_store: FakeMetadataStore,
        cas_store: FakeCASStore,
    ) -> None:
        """Rollback of a write restores original metadata."""
        from nexus.contracts.protocols.snapshot import SnapshotEntry

        now = datetime.now(UTC)
        original_meta = json.dumps(
            {
                "backend_name": "local",
                "size": 100,
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
                "version": 1,
                "zone_id": "root",
            }
        )

        entry = SnapshotEntry(
            entry_id="e-rb1",
            transaction_id="txn-rb1",
            path="/restored.txt",
            operation="write",
            original_hash="orig-hash",
            original_metadata=original_meta,
            new_hash="new-hash",
            created_at=now,
        )

        txn = FakeTransactionModel(transaction_id="txn-rb1", status="active")
        service.registry.register("txn-rb1")

        async def fake_to_thread(fn: Any, *args: Any) -> Any:
            return fn(*args)

        with (
            patch.object(service, "_load_transaction_with_entries", return_value=(txn, [entry])),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            result = await service.rollback("txn-rb1")

        assert result.status == "rolled_back"
        assert "/restored.txt" in metadata_store.data
        assert "orig-hash" in cas_store.release_calls

    @pytest.mark.asyncio
    async def test_rollback_delete_restores_file(
        self,
        service: TransactionalSnapshotService,
        metadata_store: FakeMetadataStore,
    ) -> None:
        """Rollback of a delete restores the deleted file's metadata."""
        from nexus.contracts.protocols.snapshot import SnapshotEntry

        now = datetime.now(UTC)
        original_meta = json.dumps(
            {
                "backend_name": "local",
                "size": 50,
                "created_at": now.isoformat(),
                "modified_at": now.isoformat(),
                "version": 2,
                "zone_id": "zone-1",
            }
        )

        entry = SnapshotEntry(
            entry_id="e-rb2",
            transaction_id="txn-rb2",
            path="/deleted.txt",
            operation="delete",
            original_hash="del-hash",
            original_metadata=original_meta,
            new_hash=None,
            created_at=now,
        )

        txn = FakeTransactionModel(transaction_id="txn-rb2", status="active")
        service.registry.register("txn-rb2")

        async def fake_to_thread(fn: Any, *args: Any) -> Any:
            return fn(*args)

        with (
            patch.object(service, "_load_transaction_with_entries", return_value=(txn, [entry])),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            await service.rollback("txn-rb2")

        assert "/deleted.txt" in metadata_store.data

    @pytest.mark.asyncio
    async def test_rollback_new_file_deletes_it(
        self,
        service: TransactionalSnapshotService,
        metadata_store: FakeMetadataStore,
        cas_store: FakeCASStore,
    ) -> None:
        """Rollback of a new file (no original) deletes it from metadata."""
        from nexus.contracts.protocols.snapshot import SnapshotEntry

        metadata_store.data["/new-file.txt"] = FakeMetadata(etag="new-hash")

        entry = SnapshotEntry(
            entry_id="e-rb3",
            transaction_id="txn-rb3",
            path="/new-file.txt",
            operation="write",
            original_hash=None,
            original_metadata=None,
            new_hash="new-hash",
            created_at=datetime.now(UTC),
        )

        txn = FakeTransactionModel(transaction_id="txn-rb3", status="active")
        service.registry.register("txn-rb3")

        async def fake_to_thread(fn: Any, *args: Any) -> Any:
            return fn(*args)

        with (
            patch.object(service, "_load_transaction_with_entries", return_value=(txn, [entry])),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            await service.rollback("txn-rb3")

        assert "/new-file.txt" not in metadata_store.data
        assert "new-hash" in cas_store.release_calls

    @pytest.mark.asyncio
    async def test_rollback_not_active_raises(
        self,
        service: TransactionalSnapshotService,
    ) -> None:
        """Rollback on a committed transaction raises TransactionNotActiveError."""
        txn = FakeTransactionModel(transaction_id="txn-rb4", status="committed")

        with (
            patch.object(service, "_load_transaction_with_entries", return_value=(txn, [])),
            pytest.raises(TransactionNotActiveError),
        ):
            await service.rollback("txn-rb4")


# ---------------------------------------------------------------------------
# Tests: cleanup_expired
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for expired transaction cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_expired_rolls_back(
        self,
        service: TransactionalSnapshotService,
    ) -> None:
        """Expired transactions can be rolled back."""
        txn = FakeTransactionModel(transaction_id="txn-exp1", status="active")
        service.registry.register("txn-exp1")

        async def fake_to_thread(fn: Any, *args: Any) -> Any:
            return fn(*args)

        with (
            patch.object(service, "_load_transaction_with_entries", return_value=(txn, [])),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            result = await service.rollback("txn-exp1")
            assert result.status == "rolled_back"

    @pytest.mark.asyncio
    async def test_cleanup_race_with_manual_rollback(
        self,
        service: TransactionalSnapshotService,
    ) -> None:
        """Cleanup handles gracefully when a transaction was already rolled back."""
        txn = FakeTransactionModel(transaction_id="txn-race", status="rolled_back")

        with (
            patch.object(service, "_load_transaction_with_entries", return_value=(txn, [])),
            pytest.raises(TransactionNotActiveError),
        ):
            await service.rollback("txn-race")
