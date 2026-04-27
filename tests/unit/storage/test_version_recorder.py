"""Unit tests for VersionRecorder.

Tests all write/update/delete paths and verifies field mapping
from FileMetadata (proto) to FilePathModel (SQLAlchemy).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_DIR, DT_REG, FileMetadata
from nexus.storage.models import Base, FilePathModel, VersionHistoryModel
from nexus.storage.version_recorder import VersionRecorder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with FK support."""
    eng = create_engine("sqlite:///:memory:")

    @event.listens_for(eng, "connect")
    def _set_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    """Yield a SQLAlchemy session, rollback on teardown."""
    factory = sessionmaker(bind=engine)
    sess = factory()
    yield sess
    sess.close()


def _make_metadata(
    path: str = "/test/file.txt",
    size: int = 1024,
    content_id: str | None = "sha256-abc123",
    mime_type: str | None = "text/plain",
    version: int = 1,
    zone_id: str | None = "root",
    owner_id: str | None = "owner-1",
    is_directory: bool = False,
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> FileMetadata:
    """Create a FileMetadata with sensible defaults."""
    now = datetime(2026, 2, 10, 12, 0, 0)
    return FileMetadata(
        path=path,
        size=size,
        content_id=content_id,
        mime_type=mime_type,
        version=version,
        zone_id=zone_id,
        owner_id=owner_id,
        entry_type=DT_DIR if is_directory else DT_REG,
        created_at=created_at or now,
        modified_at=modified_at or now,
    )


# ---------------------------------------------------------------------------
# TestRecordCreate
# ---------------------------------------------------------------------------


class TestRecordCreate:
    """Tests for VersionRecorder._record_create (via record_write(is_new=True))."""

    def test_creates_file_path_model(self, session: Session) -> None:
        """record_write(is_new=True) should insert a FilePathModel row."""
        metadata = _make_metadata()
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        result = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one_or_none()

        assert result is not None
        assert result.virtual_path == "/test/file.txt"

    def test_maps_all_fields_correctly(self, session: Session) -> None:
        """Every FileMetadata field should map to the correct FilePathModel column."""
        now = datetime(2026, 2, 10, 14, 30, 0)
        metadata = _make_metadata(
            path="/zone1/docs/readme.md",
            size=2048,
            content_id="sha256-xyz789",
            mime_type="text/markdown",
            zone_id="zone1",
            owner_id="user-42",
            created_at=now,
            modified_at=now,
        )

        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/zone1/docs/readme.md")
        ).scalar_one()

        # Verify field name translations (proto name -> SQLAlchemy column)
        assert fp.virtual_path == metadata.path  # path -> virtual_path
        assert fp.size_bytes == metadata.size  # size -> size_bytes
        assert fp.content_hash == metadata.content_id  # content_id -> content_hash
        assert fp.file_type == metadata.mime_type  # mime_type -> file_type
        assert fp.zone_id == metadata.zone_id
        assert fp.posix_uid == metadata.owner_id  # owner_id -> posix_uid
        assert fp.current_version == 1

    def test_creates_version_history_when_etag_present(self, session: Session) -> None:
        """When etag is set, a VersionHistoryModel entry should be created."""
        metadata = _make_metadata(content_id="sha256-abc")
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True, created_by="user-1")
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        vh = session.execute(
            select(VersionHistoryModel).where(VersionHistoryModel.resource_id == fp.path_id)
        ).scalar_one()

        assert vh.resource_type == "file"
        assert vh.version_number == 1
        assert vh.content_hash == "sha256-abc"
        assert vh.parent_version_id is None
        assert vh.source_type == "original"
        assert vh.created_by == "user-1"

    def test_no_version_history_when_etag_none(self, session: Session) -> None:
        """When etag is None, no VersionHistoryModel should be created."""
        metadata = _make_metadata(content_id=None)
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        count = session.execute(
            select(VersionHistoryModel).where(VersionHistoryModel.resource_id == fp.path_id)
        ).scalar_one_or_none()

        assert count is None

    def test_removes_soft_deleted_entry_at_same_path(self, session: Session) -> None:
        """Creating at a path with a soft-deleted entry should replace it."""
        # Create and soft-delete first entry
        first = _make_metadata(path="/test/reuse.txt")
        recorder = VersionRecorder(session)
        recorder.record_write(first, is_new=True)
        session.commit()

        recorder2 = VersionRecorder(session)
        recorder2.record_delete("/test/reuse.txt")
        session.commit()

        # Verify soft-deleted
        deleted = session.execute(
            select(FilePathModel).where(
                FilePathModel.virtual_path == "/test/reuse.txt",
                FilePathModel.deleted_at.is_not(None),
            )
        ).scalar_one_or_none()
        assert deleted is not None

        # Create new entry at same path
        second = _make_metadata(path="/test/reuse.txt", content_id="new-hash")
        recorder3 = VersionRecorder(session)
        recorder3.record_write(second, is_new=True)
        session.commit()

        # Should have exactly one non-deleted entry
        active = (
            session.execute(
                select(FilePathModel).where(
                    FilePathModel.virtual_path == "/test/reuse.txt",
                    FilePathModel.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        assert len(active) == 1
        assert active[0].content_hash == "new-hash"

    def test_duplicate_create_event_same_hash_is_idempotent(self, session: Session) -> None:
        """A repeated create event for an active path should not violate uniqueness."""
        metadata = _make_metadata(path="/test/dup.txt", etag="same-hash")
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        recorder.record_write(metadata, is_new=True)
        session.commit()

        active = (
            session.execute(
                select(FilePathModel).where(
                    FilePathModel.zone_id == metadata.zone_id,
                    FilePathModel.virtual_path == "/test/dup.txt",
                    FilePathModel.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        assert len(active) == 1
        assert active[0].current_version == 1

        versions = (
            session.execute(
                select(VersionHistoryModel).where(
                    VersionHistoryModel.resource_id == active[0].path_id
                )
            )
            .scalars()
            .all()
        )
        assert len(versions) == 1

    def test_create_event_for_existing_path_with_new_hash_updates(self, session: Session) -> None:
        """A create-labeled event for an active path with new content is an update."""
        first = _make_metadata(path="/test/dup-new-content.txt", etag="hash-1")
        second = _make_metadata(path="/test/dup-new-content.txt", etag="hash-2", size=2048)
        recorder = VersionRecorder(session)
        recorder.record_write(first, is_new=True)
        session.commit()

        recorder.record_write(second, is_new=True)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(
                FilePathModel.zone_id == first.zone_id,
                FilePathModel.virtual_path == "/test/dup-new-content.txt",
                FilePathModel.deleted_at.is_(None),
            )
        ).scalar_one()
        assert fp.content_hash == "hash-2"
        assert fp.size_bytes == 2048
        assert fp.current_version == 2

        versions = (
            session.execute(
                select(VersionHistoryModel)
                .where(VersionHistoryModel.resource_id == fp.path_id)
                .order_by(VersionHistoryModel.version_number)
            )
            .scalars()
            .all()
        )
        assert [v.content_hash for v in versions] == ["hash-1", "hash-2"]

    def test_defaults_zone_to_default(self, session: Session) -> None:
        """When zone_id is None, should default to ROOT_ZONE_ID ('root')."""
        metadata = _make_metadata(zone_id=None)
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        assert fp.zone_id == ROOT_ZONE_ID

    def test_zero_byte_create_still_gets_version_history(self, session: Session) -> None:
        """A legitimate empty-file create (size=0, etag present) must still
        get current_version=1 and a VersionHistoryModel entry so it is tracked
        in version history.  Regression guard: the FUSE phantom-version fix
        must not suppress version history for all size-0 creates."""
        metadata = _make_metadata(size=0, content_id="blake3-empty")
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        assert fp.current_version == 1
        assert fp.size_bytes == 0

        vh = session.execute(
            select(VersionHistoryModel).where(VersionHistoryModel.resource_id == fp.path_id)
        ).scalar_one()

        assert vh.version_number == 1
        assert vh.content_hash == "blake3-empty"
        assert vh.size_bytes == 0


# ---------------------------------------------------------------------------
# TestRecordUpdate
# ---------------------------------------------------------------------------


class TestRecordUpdate:
    """Tests for VersionRecorder._record_update (via record_write(is_new=False))."""

    def _create_existing(self, session: Session) -> FilePathModel:
        """Helper to create an existing file for update tests."""
        metadata = _make_metadata(content_id="original-hash")
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()
        return session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

    def test_updates_file_path_model(self, session: Session) -> None:
        """record_write(is_new=False) should update existing FilePathModel."""
        self._create_existing(session)

        updated = _make_metadata(
            size=4096,
            content_id="updated-hash",
            mime_type="application/json",
        )
        recorder = VersionRecorder(session)
        recorder.record_write(updated, is_new=False)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        assert fp.size_bytes == 4096
        assert fp.content_hash == "updated-hash"
        assert fp.file_type == "application/json"

    def test_increments_version(self, session: Session) -> None:
        """Update with etag should increment current_version."""
        self._create_existing(session)

        updated = _make_metadata(content_id="v2-hash")
        recorder = VersionRecorder(session)
        recorder.record_write(updated, is_new=False)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        assert fp.current_version == 2

    def test_creates_version_history_with_parent_link(self, session: Session) -> None:
        """Update should create a version entry linked to the previous version."""
        self._create_existing(session)

        updated = _make_metadata(content_id="v2-hash")
        recorder = VersionRecorder(session)
        recorder.record_write(updated, is_new=False)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        versions = (
            session.execute(
                select(VersionHistoryModel)
                .where(VersionHistoryModel.resource_id == fp.path_id)
                .order_by(VersionHistoryModel.version_number)
            )
            .scalars()
            .all()
        )

        assert len(versions) == 2
        assert versions[0].version_number == 1
        assert versions[0].content_hash == "original-hash"
        assert versions[1].version_number == 2
        assert versions[1].content_hash == "v2-hash"
        assert versions[1].parent_version_id == versions[0].version_id

    def test_no_version_bump_without_etag(self, session: Session) -> None:
        """Update without etag should not increment version or create history."""
        self._create_existing(session)

        updated = _make_metadata(content_id=None)
        recorder = VersionRecorder(session)
        recorder.record_write(updated, is_new=False)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        # Version should remain at 1 (no etag = no version bump)
        assert fp.current_version == 1

        # Only one version history entry (from create)
        count = (
            session.execute(
                select(VersionHistoryModel).where(VersionHistoryModel.resource_id == fp.path_id)
            )
            .scalars()
            .all()
        )
        assert len(count) == 1

    def test_fallback_to_create_when_not_found(self, session: Session) -> None:
        """If file not found in RecordStore, update should fall back to create."""
        # Don't create existing — simulate file exists in Raft but not in PG
        metadata = _make_metadata(content_id="orphan-hash")
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=False)  # is_new=False but doesn't exist
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one_or_none()

        assert fp is not None
        assert fp.content_hash == "orphan-hash"
        assert fp.current_version == 1  # Created as version 1


# ---------------------------------------------------------------------------
# TestRecordDelete
# ---------------------------------------------------------------------------


class TestRecordDelete:
    """Tests for VersionRecorder.record_delete."""

    def test_soft_deletes_existing_file(self, session: Session) -> None:
        """record_delete should set deleted_at on existing file."""
        metadata = _make_metadata()
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        recorder2 = VersionRecorder(session)
        recorder2.record_delete("/test/file.txt")
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        assert fp.deleted_at is not None

    def test_no_error_on_missing_file(self, session: Session) -> None:
        """record_delete for nonexistent file should not raise."""
        recorder = VersionRecorder(session)
        recorder.record_delete("/nonexistent/file.txt")
        session.commit()
        # Should not raise

    def test_ignores_already_deleted_files(self, session: Session) -> None:
        """record_delete should only affect non-deleted entries."""
        metadata = _make_metadata()
        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        # Delete once
        recorder2 = VersionRecorder(session)
        recorder2.record_delete("/test/file.txt")
        session.commit()

        # Delete again — should not raise
        recorder3 = VersionRecorder(session)
        recorder3.record_delete("/test/file.txt")
        session.commit()


# ---------------------------------------------------------------------------
# TestRecordRename
# ---------------------------------------------------------------------------


class TestRecordRename:
    """Tests for VersionRecorder.record_rename."""

    def test_rename_to_existing_destination_retires_old_row(self, session: Session) -> None:
        """A delayed rename event should not violate the active path uniqueness index."""
        recorder = VersionRecorder(session)
        old_metadata = _make_metadata(path="/test/old.txt", etag="old-hash", zone_id="root")
        new_metadata = _make_metadata(path="/test/new.txt", etag="new-hash", zone_id="root")
        recorder.record_write(old_metadata, is_new=True)
        recorder.record_write(new_metadata, is_new=True)
        session.commit()

        recorder.record_rename("/test/old.txt", "/test/new.txt", zone_id="root")
        session.commit()

        old_row = session.execute(
            select(FilePathModel).where(
                FilePathModel.zone_id == "root",
                FilePathModel.virtual_path == "/test/old.txt",
            )
        ).scalar_one()
        active_new_rows = (
            session.execute(
                select(FilePathModel).where(
                    FilePathModel.zone_id == "root",
                    FilePathModel.virtual_path == "/test/new.txt",
                    FilePathModel.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )

        assert old_row.deleted_at is not None
        assert len(active_new_rows) == 1
        assert active_new_rows[0].content_hash == "new-hash"


# ---------------------------------------------------------------------------
# TestTimestampHandling
# ---------------------------------------------------------------------------


class TestTimestampHandling:
    """Tests for timezone-aware to naive datetime conversion."""

    def test_timezone_aware_timestamps_stored_as_naive(self, session: Session) -> None:
        """Timezone-aware datetimes should be stored as naive UTC (SQLite compat)."""
        aware_time = datetime(2026, 6, 15, 10, 30, 0, tzinfo=UTC)
        metadata = _make_metadata(created_at=aware_time, modified_at=aware_time)

        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        # Should be stored without timezone info
        assert fp.created_at.tzinfo is None
        assert fp.created_at.year == 2026
        assert fp.created_at.month == 6
        assert fp.created_at.hour == 10

    def test_none_timestamps_default_to_now(self, session: Session) -> None:
        """None timestamps should default to current UTC time."""
        metadata = _make_metadata(created_at=None, modified_at=None)

        recorder = VersionRecorder(session)
        recorder.record_write(metadata, is_new=True)
        session.commit()

        fp = session.execute(
            select(FilePathModel).where(FilePathModel.virtual_path == "/test/file.txt")
        ).scalar_one()

        assert fp.created_at is not None
        assert fp.updated_at is not None
