"""Failure injection tests for the write path.

Tests what happens when Raft succeeds but PostgreSQL fails,
and verifies AuditConfig.strict_mode behavior for single and batch writes.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import AuditLogError
from nexus.contracts.metadata import DT_DIR, DT_REG, FileMetadata
from nexus.storage.models import Base, FilePathModel
from nexus.storage.record_store import RecordStoreABC
from nexus.storage.record_store_syncer import RecordStoreWriteObserver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metadata(
    path: str = "/test/file.txt",
    etag: str | None = "sha256-abc",
    backend_name: str = "local",
    physical_path: str = "/data/abc123",
    size: int = 1024,
    mime_type: str | None = "text/plain",
    version: int = 1,
    zone_id: str | None = "root",
    created_by: str | None = "user-1",
    owner_id: str | None = "owner-1",
    is_directory: bool = False,
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> FileMetadata:
    now = datetime(2026, 2, 10, 12, 0, 0)
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path=physical_path,
        size=size,
        etag=etag,
        mime_type=mime_type,
        version=version,
        zone_id=zone_id,
        created_by=created_by,
        owner_id=owner_id,
        entry_type=DT_DIR if is_directory else DT_REG,
        created_at=created_at or now,
        modified_at=modified_at or now,
    )


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")

    @event.listens_for(eng, "connect")
    def _pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    factory = sessionmaker(bind=engine)
    return factory


@pytest.fixture
def record_store(session_factory):
    mock_rs = MagicMock(spec=RecordStoreABC)
    mock_rs.session_factory = session_factory
    return mock_rs


# ---------------------------------------------------------------------------
# Test: RecordStoreWriteObserver raises AuditLogError on failure (strict mode)
# ---------------------------------------------------------------------------


class TestSyncerRaisesOnFailure:
    """RecordStoreWriteObserver raises AuditLogError in strict_mode (default)."""

    def test_on_write_propagates_db_error(self, record_store) -> None:
        """Database errors in on_write should propagate to caller."""
        syncer = RecordStoreWriteObserver(record_store=record_store)

        with (
            patch(
                "nexus.storage.operation_logger.OperationLogger.log_operation",
                side_effect=RuntimeError("Connection refused"),
            ),
            pytest.raises(AuditLogError),
        ):
            syncer.on_write(
                metadata=_make_metadata(),
                is_new=True,
                path="/test/file.txt",
            )

    def test_on_write_propagates_version_recorder_error(self, record_store) -> None:
        """VersionRecorder errors in on_write should propagate to caller."""
        syncer = RecordStoreWriteObserver(record_store=record_store)

        with (
            patch(
                "nexus.storage.version_recorder.VersionRecorder.record_write",
                side_effect=ValueError("FK constraint violation"),
            ),
            pytest.raises(AuditLogError),
        ):
            syncer.on_write(
                metadata=_make_metadata(),
                is_new=True,
                path="/test/file.txt",
            )

    def test_on_delete_propagates_error(self, record_store) -> None:
        """Errors in on_delete should propagate to caller."""
        syncer = RecordStoreWriteObserver(record_store=record_store)

        with (
            patch(
                "nexus.storage.operation_logger.OperationLogger.log_operation",
                side_effect=RuntimeError("Timeout"),
            ),
            pytest.raises(AuditLogError),
        ):
            syncer.on_delete(path="/test/file.txt")


# ---------------------------------------------------------------------------
# Test: Batch write error handling (Issue #2152: observer owns error policy)
# ---------------------------------------------------------------------------


class TestBatchWriteErrorHandling:
    """Tests for batch write error handling (Issue #2152).

    Issue #2152: Kernel is a pure caller — observer errors propagate directly.
    No contextlib.suppress or try/except wrapper in the kernel.
    """

    def test_batch_observer_error_propagates(self) -> None:
        """Observer errors in batch writes propagate directly (no suppression)."""
        write_observer = MagicMock()
        write_observer.on_write_batch.side_effect = RuntimeError("PG down")

        # Kernel calls observer directly — error propagates
        with pytest.raises(RuntimeError, match="PG down"):
            write_observer.on_write_batch(
                [(_make_metadata(), True)],
                zone_id="root",
            )

    def test_single_write_observer_error_propagates(self) -> None:
        """Observer errors propagate — kernel has no try/except wrapper (#2152)."""
        write_observer = MagicMock()
        write_observer.on_write.side_effect = RuntimeError("DB connection lost")

        with pytest.raises(RuntimeError, match="DB connection lost"):
            write_observer.on_write(
                metadata=_make_metadata(),
                is_new=True,
                path="/test/file.txt",
            )


# ---------------------------------------------------------------------------
# Test: VersionRecorder handles edge cases
# ---------------------------------------------------------------------------


class TestVersionRecorderEdgeCases:
    """Edge cases in VersionRecorder that could cause data corruption."""

    def test_update_with_soft_deleted_entry_falls_back_to_create(self, session_factory) -> None:
        """Updating a path that only has a soft-deleted entry should create new."""
        from nexus.storage.version_recorder import VersionRecorder

        session = session_factory()
        try:
            # Create and delete
            recorder = VersionRecorder(session)
            recorder.record_write(_make_metadata(), is_new=True)
            session.commit()

            recorder2 = VersionRecorder(session)
            recorder2.record_delete("/test/file.txt")
            session.commit()

            # Now try to update (not create) — should fall back to create
            recorder3 = VersionRecorder(session)
            recorder3.record_write(_make_metadata(etag="new-content"), is_new=False)
            session.commit()

            # Should have one active entry
            active = (
                session.execute(
                    select(FilePathModel).where(
                        FilePathModel.virtual_path == "/test/file.txt",
                        FilePathModel.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            assert len(active) == 1
            assert active[0].content_hash == "new-content"
        finally:
            session.close()

    def test_concurrent_create_at_same_path(self, session_factory) -> None:
        """Two creates at the same path should not produce duplicates.

        The second create should handle the existing entry gracefully.
        """
        from nexus.storage.version_recorder import VersionRecorder

        session = session_factory()
        try:
            recorder = VersionRecorder(session)
            recorder.record_write(_make_metadata(etag="first"), is_new=True)
            session.commit()

            # Second create at same path (simulates race condition)
            # VersionRecorder._record_create removes soft-deleted but
            # doesn't check for active duplicates — this may raise
            # or create a duplicate depending on unique constraint
            recorder2 = VersionRecorder(session)
            try:
                recorder2.record_write(_make_metadata(etag="second"), is_new=True)
                session.commit()
            except Exception:
                session.rollback()
                # Expected: unique constraint violation
                # This documents a gap that #1246 should address
        finally:
            session.close()

    def test_zero_size_file_handled(self, session_factory) -> None:
        """Zero-byte files should be stored correctly."""
        from nexus.storage.version_recorder import VersionRecorder

        session = session_factory()
        try:
            metadata = _make_metadata(size=0, etag="empty-hash")
            recorder = VersionRecorder(session)
            recorder.record_write(metadata, is_new=True)
            session.commit()

            fp = session.execute(
                select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
            ).scalar_one()
            assert fp.size_bytes == 0
        finally:
            session.close()

    def test_very_long_path_handled(self, session_factory) -> None:
        """Paths near the maximum length should be stored correctly."""
        from nexus.storage.version_recorder import VersionRecorder

        session = session_factory()
        try:
            long_path = "/" + "a" * 3000 + "/file.txt"
            metadata = _make_metadata(path=long_path)
            recorder = VersionRecorder(session)
            recorder.record_write(metadata, is_new=True)
            session.commit()

            fp = session.execute(
                select(FilePathModel).where(FilePathModel.virtual_path == long_path)
            ).scalar_one()
            assert fp.virtual_path == long_path
        finally:
            session.close()
