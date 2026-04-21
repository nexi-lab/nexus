"""Unit tests for TransactionalSnapshotService (Issue #1752).

Tests: Init, IsTracked, Begin, TrackWrite, TrackDelete, Commit, Rollback,
       Get, List, Cleanup, Performance, FailureInjection.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nexus.bricks.snapshot.service import (
    TransactionalSnapshotService,
    TransactionConflictError,
    TransactionNotActiveError,
    TransactionNotFoundError,
)
from nexus.contracts.constants import ROOT_ZONE_ID


class TestInit:
    """Tests for service initialization."""

    def test_init_stores_dependencies(
        self,
        mock_record_store: MagicMock,
        mock_cas_store: MagicMock,
        mock_metadata_store: MagicMock,
        mock_session_factory: MagicMock,
    ) -> None:
        svc = TransactionalSnapshotService(
            record_store=mock_record_store,
            cas_store=mock_cas_store,
            metadata_store=mock_metadata_store,
        )
        assert svc._session_factory is mock_session_factory
        assert svc._cas_store is mock_cas_store
        assert svc._metadata_store is mock_metadata_store

    def test_init_creates_registry(self, snapshot_service: TransactionalSnapshotService) -> None:
        assert snapshot_service.registry is not None
        assert snapshot_service.registry.active_count == 0


class TestIsTracked:
    """Tests for is_tracked() fast-path."""

    def test_not_tracked_when_no_transactions(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        assert snapshot_service.is_tracked("/file.txt") is None

    def test_not_tracked_when_path_not_registered(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        snapshot_service.registry.register("txn-1")
        assert snapshot_service.is_tracked("/file.txt") is None

    def test_tracked_after_registration(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        snapshot_service.registry.register("txn-1")
        snapshot_service.registry.track_path("txn-1", "/file.txt")
        assert snapshot_service.is_tracked("/file.txt") == "txn-1"

    def test_is_tracked_performance_no_transactions(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        """is_tracked() should be < 5us with no active transactions."""
        iterations = 10_000
        start = time.perf_counter_ns()
        for _ in range(iterations):
            snapshot_service.is_tracked("/some/path.txt")
        elapsed_ns = time.perf_counter_ns() - start
        avg_ns = elapsed_ns / iterations
        # Allow generous margin for CI: 50us
        assert avg_ns < 50_000, f"is_tracked() too slow: {avg_ns}ns avg"


class TestBegin:
    """Tests for begin()."""

    @pytest.mark.asyncio
    async def test_begin_creates_transaction(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        info = await snapshot_service.begin(zone_id="zone-1", agent_id="agent-1")
        assert info.zone_id == "zone-1"
        assert info.agent_id == "agent-1"
        assert info.status == "active"
        assert info.entry_count == 0
        assert snapshot_service.registry.has_active_transactions()

    @pytest.mark.asyncio
    async def test_begin_with_description(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        info = await snapshot_service.begin(zone_id="zone-1", description="Test transaction")
        assert info.description == "Test transaction"

    @pytest.mark.asyncio
    async def test_begin_with_custom_ttl(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        info = await snapshot_service.begin(zone_id="zone-1", ttl_seconds=300)
        assert info.expires_at > info.created_at

    @pytest.mark.asyncio
    async def test_begin_registers_in_memory(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        await snapshot_service.begin(zone_id="zone-1")
        assert snapshot_service.registry.active_count == 1

    @pytest.mark.asyncio
    async def test_begin_multiple_transactions(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        await snapshot_service.begin(zone_id="zone-1")
        await snapshot_service.begin(zone_id="zone-1")
        assert snapshot_service.registry.active_count == 2


class TestTrackWrite:
    """Tests for track_write()."""

    @pytest.mark.asyncio
    async def test_track_write_holds_cas_reference(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_cas_store: MagicMock,
    ) -> None:
        info = await snapshot_service.begin(zone_id="zone-1")
        # Manually register path first
        snapshot_service.registry.track_path(info.transaction_id, "/file.txt")

        snapshot_service.track_write(
            info.transaction_id,
            "/file.txt",
            original_hash="abc123",
            original_metadata={"size": 100},
            new_hash="def456",
        )
        mock_cas_store.hold_reference.assert_called_with("abc123")

    @pytest.mark.asyncio
    async def test_track_write_no_hold_for_new_files(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_cas_store: MagicMock,
    ) -> None:
        info = await snapshot_service.begin(zone_id="zone-1")
        snapshot_service.track_write(
            info.transaction_id,
            "/new-file.txt",
            original_hash=None,
            original_metadata=None,
            new_hash="abc123",
        )
        mock_cas_store.hold_reference.assert_not_called()

    @pytest.mark.asyncio
    async def test_track_write_persists_entry(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_session_factory: MagicMock,
    ) -> None:
        info = await snapshot_service.begin(zone_id="zone-1")
        snapshot_service.track_write(
            info.transaction_id,
            "/file.txt",
            original_hash="abc123",
            original_metadata={"size": 100},
            new_hash="def456",
        )
        # Session factory should have been called for the entry persist
        assert mock_session_factory.call_count >= 2  # begin + track_write

    @pytest.mark.asyncio
    async def test_track_write_conflict_with_different_txn(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_cas_store: MagicMock,
    ) -> None:
        info1 = await snapshot_service.begin(zone_id="zone-1")
        info2 = await snapshot_service.begin(zone_id="zone-1")

        # First transaction tracks the path
        snapshot_service.track_write(
            info1.transaction_id, "/file.txt", "hash1", {"size": 1}, "hash2"
        )

        # Second transaction tries same path — should raise conflict
        mock_cas_store.hold_reference.reset_mock()
        with pytest.raises(TransactionConflictError) as exc_info:
            snapshot_service.track_write(
                info2.transaction_id, "/file.txt", "hash2", {"size": 2}, "hash3"
            )
        # CAS release should be called since tracking failed
        mock_cas_store.release.assert_called_with("hash2")
        assert len(exc_info.value.conflicts) == 1
        assert exc_info.value.conflicts[0].path == "/file.txt"


class TestTrackDelete:
    """Tests for track_delete()."""

    @pytest.mark.asyncio
    async def test_track_delete_holds_cas_reference(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_cas_store: MagicMock,
    ) -> None:
        info = await snapshot_service.begin(zone_id="zone-1")
        snapshot_service.track_delete(
            info.transaction_id,
            "/file.txt",
            original_hash="abc123",
            original_metadata={"size": 100, "version": 1},
        )
        mock_cas_store.hold_reference.assert_called_with("abc123")


class TestCommit:
    """Tests for commit()."""

    @pytest.mark.asyncio
    async def test_commit_not_found(self, snapshot_service: TransactionalSnapshotService) -> None:
        with pytest.raises(TransactionNotFoundError):
            await snapshot_service.commit("nonexistent")

    @pytest.mark.asyncio
    async def test_commit_not_active(self, snapshot_service: TransactionalSnapshotService) -> None:
        """Commit on a non-active transaction should raise."""
        # We need to set up a committed transaction in the mock store
        from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

        model = TransactionSnapshotModel(
            transaction_id="txn-committed",
            zone_id=ROOT_ZONE_ID,
            status="committed",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            entry_count=0,
        )
        # Patch the session to return this model
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.get.return_value = model
        session.execute.return_value.scalars.return_value.all.return_value = []
        snapshot_service._session_factory.return_value = session

        with pytest.raises(TransactionNotActiveError):
            await snapshot_service.commit("txn-committed")

    @pytest.mark.asyncio
    async def test_commit_releases_cas_holds(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_cas_store: MagicMock,
    ) -> None:
        """Commit should release CAS holds on original content."""
        from nexus.storage.models.transaction_snapshot import (
            SnapshotEntryModel,
            TransactionSnapshotModel,
        )

        txn_model = TransactionSnapshotModel(
            transaction_id="txn-1",
            zone_id=ROOT_ZONE_ID,
            status="active",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            entry_count=1,
        )
        entry_model = SnapshotEntryModel(
            entry_id="entry-1",
            transaction_id="txn-1",
            path="/file.txt",
            operation="write",
            original_hash="old-hash",
            new_hash="new-hash",
            created_at=datetime.now(UTC),
        )

        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.get.return_value = txn_model
        session.execute.return_value.scalars.return_value.all.return_value = [entry_model]
        session.commit = MagicMock()
        session.refresh = MagicMock()
        snapshot_service._session_factory.return_value = session

        # Mock metadata store to return matching hash (no conflict)
        meta = MagicMock()
        meta.etag = "new-hash"
        snapshot_service._metadata_store.get.return_value = meta

        snapshot_service.registry.register("txn-1")
        await snapshot_service.commit("txn-1")

        mock_cas_store.release.assert_called_with("old-hash")
        assert not snapshot_service.registry.has_active_transactions()

    @pytest.mark.asyncio
    async def test_commit_detects_conflict(
        self,
        snapshot_service: TransactionalSnapshotService,
    ) -> None:
        """Commit should raise TransactionConflictError when file was modified externally."""
        from nexus.storage.models.transaction_snapshot import (
            SnapshotEntryModel,
            TransactionSnapshotModel,
        )

        txn_model = TransactionSnapshotModel(
            transaction_id="txn-1",
            zone_id=ROOT_ZONE_ID,
            status="active",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            entry_count=1,
        )
        entry_model = SnapshotEntryModel(
            entry_id="entry-1",
            transaction_id="txn-1",
            path="/file.txt",
            operation="write",
            original_hash="old-hash",
            new_hash="expected-hash",
            created_at=datetime.now(UTC),
        )

        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.get.return_value = txn_model
        session.execute.return_value.scalars.return_value.all.return_value = [entry_model]
        snapshot_service._session_factory.return_value = session

        # Metadata store returns different hash (conflict!)
        meta = MagicMock()
        meta.etag = "different-hash"
        snapshot_service._metadata_store.get.return_value = meta

        snapshot_service.registry.register("txn-1")
        with pytest.raises(TransactionConflictError) as exc_info:
            await snapshot_service.commit("txn-1")

        assert len(exc_info.value.conflicts) == 1
        assert exc_info.value.conflicts[0].path == "/file.txt"


class TestRollback:
    """Tests for rollback()."""

    @pytest.mark.asyncio
    async def test_rollback_not_found(self, snapshot_service: TransactionalSnapshotService) -> None:
        with pytest.raises(TransactionNotFoundError):
            await snapshot_service.rollback("nonexistent")

    @pytest.mark.asyncio
    async def test_rollback_restores_metadata(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_cas_store: MagicMock,
        mock_metadata_store: MagicMock,
    ) -> None:
        """Rollback should restore file metadata from snapshot."""
        from nexus.storage.models.transaction_snapshot import (
            SnapshotEntryModel,
            TransactionSnapshotModel,
        )

        now = datetime.now(UTC)
        txn_model = TransactionSnapshotModel(
            transaction_id="txn-1",
            zone_id=ROOT_ZONE_ID,
            status="active",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            entry_count=1,
        )
        entry_model = SnapshotEntryModel(
            entry_id="entry-1",
            transaction_id="txn-1",
            path="/file.txt",
            operation="write",
            original_hash="original-hash",
            original_metadata=json.dumps(
                {
                    "size": 100,
                    "version": 1,
                    "created_at": now.isoformat(),
                    "modified_at": now.isoformat(),
                    "zone_id": "root",
                }
            ),
            new_hash="new-hash",
            created_at=now,
        )

        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.get.return_value = txn_model
        session.execute.return_value.scalars.return_value.all.return_value = [entry_model]
        session.commit = MagicMock()
        session.refresh = MagicMock()
        snapshot_service._session_factory.return_value = session
        snapshot_service.registry.register("txn-1")

        await snapshot_service.rollback("txn-1")

        # Should have called metadata_store.put to restore
        mock_metadata_store.put.assert_called_once()
        restored = mock_metadata_store.put.call_args[0][0]
        assert restored.path == "/file.txt"
        assert restored.etag == "original-hash"

        # Should release CAS hold
        mock_cas_store.release.assert_called_with("original-hash")

    @pytest.mark.asyncio
    async def test_rollback_new_file_deletes_it(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_metadata_store: MagicMock,
        mock_cas_store: MagicMock,
    ) -> None:
        """Rollback of a new file (original_hash=None) should delete it."""
        from nexus.storage.models.transaction_snapshot import (
            SnapshotEntryModel,
            TransactionSnapshotModel,
        )

        now = datetime.now(UTC)
        txn_model = TransactionSnapshotModel(
            transaction_id="txn-1",
            zone_id=ROOT_ZONE_ID,
            status="active",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            entry_count=1,
        )
        entry_model = SnapshotEntryModel(
            entry_id="entry-1",
            transaction_id="txn-1",
            path="/new-file.txt",
            operation="write",
            original_hash=None,
            original_metadata=None,
            new_hash="new-hash",
            created_at=now,
        )

        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.get.return_value = txn_model
        session.execute.return_value.scalars.return_value.all.return_value = [entry_model]
        session.commit = MagicMock()
        session.refresh = MagicMock()
        snapshot_service._session_factory.return_value = session
        snapshot_service.registry.register("txn-1")

        await snapshot_service.rollback("txn-1")

        mock_metadata_store.delete.assert_called_with("/new-file.txt")
        mock_cas_store.release.assert_called_with("new-hash")


class TestGetTransaction:
    """Tests for get_transaction()."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.get.return_value = None
        snapshot_service._session_factory.return_value = session

        result = await snapshot_service.get_transaction("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_existing_transaction(
        self, snapshot_service: TransactionalSnapshotService
    ) -> None:
        from nexus.storage.models.transaction_snapshot import TransactionSnapshotModel

        now = datetime.now(UTC)
        model = TransactionSnapshotModel(
            transaction_id="txn-1",
            zone_id=ROOT_ZONE_ID,
            status="active",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            entry_count=0,
        )
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.get.return_value = model
        snapshot_service._session_factory.return_value = session

        result = await snapshot_service.get_transaction("txn-1")
        assert result is not None
        assert result.transaction_id == "txn-1"
        assert result.status == "active"


class TestListTransactions:
    """Tests for list_transactions()."""

    @pytest.mark.asyncio
    async def test_list_empty(self, snapshot_service: TransactionalSnapshotService) -> None:
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.execute.return_value.scalars.return_value.all.return_value = []
        snapshot_service._session_factory.return_value = session

        result = await snapshot_service.list_transactions(zone_id=ROOT_ZONE_ID)
        assert result == []


class TestCleanup:
    """Tests for cleanup_expired()."""

    @pytest.mark.asyncio
    async def test_cleanup_no_expired(self, snapshot_service: TransactionalSnapshotService) -> None:
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.execute.return_value.scalars.return_value.all.return_value = []
        snapshot_service._session_factory.return_value = session

        cleaned = await snapshot_service.cleanup_expired()
        assert cleaned == 0


class TestFailureInjection:
    """Failure injection tests for error handling."""

    @pytest.mark.asyncio
    async def test_cas_hold_fails_logs_warning(
        self,
        snapshot_service: TransactionalSnapshotService,
        mock_cas_store: MagicMock,
    ) -> None:
        """When CAS hold_reference fails, it should log but still track."""
        mock_cas_store.hold_reference.return_value = False
        info = await snapshot_service.begin(zone_id="zone-1")
        # Should not raise
        snapshot_service.track_write(
            info.transaction_id, "/file.txt", "bad-hash", {"size": 1}, "new-hash"
        )

    @pytest.mark.asyncio
    async def test_db_write_fails_during_begin(
        self,
        mock_cas_store: MagicMock,
        mock_metadata_store: MagicMock,
    ) -> None:
        """If DB write fails during begin(), no registry entry should be created."""
        factory = MagicMock()
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.add.side_effect = RuntimeError("DB unavailable")
        factory.return_value = session

        svc = TransactionalSnapshotService(
            record_store=MagicMock(session_factory=factory),
            cas_store=mock_cas_store,
            metadata_store=mock_metadata_store,
        )

        with pytest.raises(RuntimeError, match="DB unavailable"):
            await svc.begin(zone_id="zone-1")

        # Registry should be clean since begin() failed before register
        assert svc.registry.active_count == 0
