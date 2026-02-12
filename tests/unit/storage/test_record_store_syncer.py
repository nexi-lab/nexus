"""Unit tests for RecordStoreSyncer — the bridge between Metastore and RecordStore.

Tests happy paths, SQL failure injection, partial failures, and session exhaustion.
Phase 1.1 of #1246/#1330 consolidation plan.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from nexus.core._metadata_generated import FileMetadata
from nexus.storage.models import FilePathModel, OperationLogModel, VersionHistoryModel
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.record_store_syncer import RecordStoreSyncer


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db")
    yield rs
    rs.close()


@pytest.fixture
def syncer(record_store: SQLAlchemyRecordStore) -> RecordStoreSyncer:
    return RecordStoreSyncer(record_store.session_factory)


def _make_metadata(
    path: str = "/test.txt",
    *,
    etag: str = "abc123",
    size: int = 100,
    version: int = 1,
    zone_id: str = "default",
    owner_id: str | None = "user1",
) -> FileMetadata:
    """Create a FileMetadata for testing."""
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=etag,
        size=size,
        etag=etag,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        zone_id=zone_id,
        created_by="test_user",
        owner_id=owner_id,
    )


# =========================================================================
# Happy path tests
# =========================================================================


class TestOnWriteHappyPath:
    """Test on_write() creates OperationLog + FilePathModel + VersionHistory."""

    def test_new_file_creates_all_records(
        self, syncer: RecordStoreSyncer, record_store: SQLAlchemyRecordStore
    ) -> None:
        metadata = _make_metadata("/new.txt", etag="hash1")
        syncer.on_write(metadata, is_new=True, path="/new.txt", zone_id="default")

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].operation_type == "write"
            assert ops[0].path == "/new.txt"

            fps = session.query(FilePathModel).filter(FilePathModel.deleted_at.is_(None)).all()
            assert len(fps) == 1
            assert fps[0].virtual_path == "/new.txt"
            assert fps[0].content_hash == "hash1"
            assert fps[0].current_version == 1

            vhs = session.query(VersionHistoryModel).all()
            assert len(vhs) == 1
            assert vhs[0].version_number == 1
            assert vhs[0].content_hash == "hash1"

    def test_update_file_increments_version(
        self, syncer: RecordStoreSyncer, record_store: SQLAlchemyRecordStore
    ) -> None:
        # Create initial file
        m1 = _make_metadata("/file.txt", etag="v1hash")
        syncer.on_write(m1, is_new=True, path="/file.txt", zone_id="default")

        # Update file
        m2 = _make_metadata("/file.txt", etag="v2hash", version=2)
        syncer.on_write(m2, is_new=False, path="/file.txt", zone_id="default")

        with record_store.session_factory() as session:
            fp = (
                session.query(FilePathModel)
                .filter(
                    FilePathModel.virtual_path == "/file.txt",
                    FilePathModel.deleted_at.is_(None),
                )
                .one()
            )
            assert fp.current_version == 2
            assert fp.content_hash == "v2hash"

            vhs = (
                session.query(VersionHistoryModel)
                .filter(
                    VersionHistoryModel.resource_id == fp.path_id,
                )
                .order_by(VersionHistoryModel.version_number)
                .all()
            )
            assert len(vhs) == 2
            assert vhs[0].version_number == 1
            assert vhs[1].version_number == 2
            # Version 2 should have parent link to version 1
            assert vhs[1].parent_version_id == vhs[0].version_id


class TestOnDeleteHappyPath:
    """Test on_delete() soft-deletes FilePathModel."""

    def test_delete_soft_deletes(
        self, syncer: RecordStoreSyncer, record_store: SQLAlchemyRecordStore
    ) -> None:
        # Create file first
        m = _make_metadata("/del.txt", etag="delhash")
        syncer.on_write(m, is_new=True, path="/del.txt", zone_id="default")

        # Delete it
        syncer.on_delete(path="/del.txt", zone_id="default")

        with record_store.session_factory() as session:
            # Should be soft-deleted (deleted_at set)
            fp = (
                session.query(FilePathModel)
                .filter(
                    FilePathModel.virtual_path == "/del.txt",
                )
                .one()
            )
            assert fp.deleted_at is not None

            # Operation log should have delete entry
            ops = (
                session.query(OperationLogModel)
                .filter(
                    OperationLogModel.operation_type == "delete",
                )
                .all()
            )
            assert len(ops) == 1

    def test_delete_nonexistent_is_noop(
        self, syncer: RecordStoreSyncer, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Deleting a file that doesn't exist in RecordStore should not raise."""
        syncer.on_delete(path="/nonexistent.txt", zone_id="default")

        with record_store.session_factory() as session:
            ops = (
                session.query(OperationLogModel)
                .filter(
                    OperationLogModel.operation_type == "delete",
                )
                .all()
            )
            assert len(ops) == 1  # Operation logged even if file didn't exist


class TestOnRenameHappyPath:
    """Test on_rename() logs the rename operation."""

    def test_rename_logged(
        self, syncer: RecordStoreSyncer, record_store: SQLAlchemyRecordStore
    ) -> None:
        syncer.on_rename(
            old_path="/old.txt",
            new_path="/new.txt",
            zone_id="default",
            agent_id="agent1",
        )

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].operation_type == "rename"
            assert ops[0].path == "/old.txt"
            assert ops[0].new_path == "/new.txt"


class TestOnWriteBatchHappyPath:
    """Test on_write_batch() handles multiple items in one transaction."""

    def test_batch_creates_all_records(
        self, syncer: RecordStoreSyncer, record_store: SQLAlchemyRecordStore
    ) -> None:
        items = [
            (_make_metadata("/a.txt", etag="ha"), True),
            (_make_metadata("/b.txt", etag="hb"), True),
            (_make_metadata("/c.txt", etag="hc"), True),
        ]
        syncer.on_write_batch(items, zone_id="default")

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 3

            fps = session.query(FilePathModel).filter(FilePathModel.deleted_at.is_(None)).all()
            assert len(fps) == 3

            paths = {fp.virtual_path for fp in fps}
            assert paths == {"/a.txt", "/b.txt", "/c.txt"}


# =========================================================================
# Failure injection tests
# =========================================================================


class TestSQLFailure:
    """Test behavior when SQL operations fail."""

    def test_session_commit_failure_raises(self, record_store: SQLAlchemyRecordStore) -> None:
        """When session.commit() fails, the exception should propagate."""
        mock_session = MagicMock()
        mock_session.commit.side_effect = OperationalError("connection lost", {}, None)

        def failing_session_factory():
            return mock_session

        # RecordStoreSyncer uses context manager protocol
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        syncer = RecordStoreSyncer(failing_session_factory)
        metadata = _make_metadata()

        with pytest.raises(OperationalError):
            syncer.on_write(metadata, is_new=True, path="/test.txt")

    def test_delete_with_session_failure_raises(self, record_store: SQLAlchemyRecordStore) -> None:
        mock_session = MagicMock()
        mock_session.commit.side_effect = OperationalError("connection lost", {}, None)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        syncer = RecordStoreSyncer(lambda: mock_session)

        with pytest.raises(OperationalError):
            syncer.on_delete(path="/test.txt")


class TestPartialFailure:
    """Test behavior when one sub-component fails mid-transaction."""

    def test_version_recorder_failure_prevents_commit(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """If VersionRecorder raises, the entire transaction should fail."""
        syncer = RecordStoreSyncer(record_store.session_factory)
        metadata = _make_metadata()

        with (
            patch(
                "nexus.storage.version_recorder.VersionRecorder.record_write",
                side_effect=ValueError("simulated version recorder failure"),
            ),
            pytest.raises(ValueError, match="simulated version recorder failure"),
        ):
            syncer.on_write(metadata, is_new=True, path="/test.txt")

        # Verify nothing was committed (transaction rolled back)
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 0

            fps = session.query(FilePathModel).all()
            assert len(fps) == 0

    def test_operation_logger_failure_prevents_commit(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """If OperationLogger raises, nothing should be committed."""
        syncer = RecordStoreSyncer(record_store.session_factory)
        metadata = _make_metadata()

        with (
            patch(
                "nexus.storage.operation_logger.OperationLogger.log_operation",
                side_effect=ValueError("simulated logger failure"),
            ),
            pytest.raises(ValueError, match="simulated logger failure"),
        ):
            syncer.on_write(metadata, is_new=True, path="/test.txt")

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 0

            fps = session.query(FilePathModel).all()
            assert len(fps) == 0


class TestSessionFactoryFailure:
    """Test behavior when session factory itself fails."""

    def test_session_factory_raises(self) -> None:
        """If the session factory raises, the exception propagates."""

        def failing_factory():
            raise ConnectionError("database unavailable")

        syncer = RecordStoreSyncer(failing_factory)
        metadata = _make_metadata()

        with pytest.raises(ConnectionError, match="database unavailable"):
            syncer.on_write(metadata, is_new=True, path="/test.txt")


class TestBatchPartialFailure:
    """Test batch operations with mid-batch failures."""

    def test_batch_failure_rolls_back_all(self, record_store: SQLAlchemyRecordStore) -> None:
        """If batch write fails partway through, no items should be committed."""
        syncer = RecordStoreSyncer(record_store.session_factory)

        items = [
            (_make_metadata("/a.txt", etag="ha"), True),
            (_make_metadata("/b.txt", etag="hb"), True),
        ]

        call_count = 0
        original_record_write = None

        def failing_on_second_call(self_inner, metadata, *, is_new):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("simulated failure on second item")
            return original_record_write(self_inner, metadata, is_new=is_new)

        from nexus.storage.version_recorder import VersionRecorder

        original_record_write = VersionRecorder.record_write

        with (
            patch.object(VersionRecorder, "record_write", failing_on_second_call),
            pytest.raises(ValueError, match="simulated failure on second item"),
        ):
            syncer.on_write_batch(items, zone_id="default")

        # Nothing committed
        with record_store.session_factory() as session:
            fps = session.query(FilePathModel).all()
            assert len(fps) == 0
