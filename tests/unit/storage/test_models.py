"""Unit tests for SQLAlchemy models."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from nexus.storage.models import Base, FileMetadataModel, FilePathModel


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine for testing."""
    engine = create_engine("sqlite:///:memory:")

    # Enable foreign key constraints for SQLite
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create a database session for testing."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


class TestFilePathModel:
    """Test suite for FilePathModel."""

    def test_create_file_path(self, session):
        """Test creating a file path record."""
        file_path = FilePathModel(
            virtual_path="/test/file.txt",
            size_bytes=1024,
            content_hash="abc123",
            file_type="text/plain",
        )
        session.add(file_path)
        session.commit()

        assert file_path.path_id is not None
        # v0.5.0: zone_id removed - use ReBAC for multi-zone access control
        assert file_path.virtual_path == "/test/file.txt"
        assert file_path.created_at is not None
        assert file_path.updated_at is not None

    def test_soft_delete(self, session):
        """Test soft delete functionality."""
        file_path = FilePathModel(
            virtual_path="/test/file.txt",
            size_bytes=1024,
        )
        session.add(file_path)
        session.commit()

        # Soft delete
        file_path.deleted_at = datetime.now(UTC)
        session.commit()

        assert file_path.deleted_at is not None

    def test_relationship_with_metadata(self, session):
        """Test relationship between FilePathModel and FileMetadataModel."""
        file_path = FilePathModel(
            virtual_path="/test/file.txt",
            size_bytes=1024,
        )
        session.add(file_path)
        session.commit()

        # Add metadata
        metadata = FileMetadataModel(path_id=file_path.path_id, key="author", value='"John Doe"')
        session.add(metadata)
        session.commit()

        # Test relationship
        assert len(file_path.metadata_entries) == 1
        assert file_path.metadata_entries[0].key == "author"

    def test_cascade_delete_metadata(self, session):
        """Test that deleting file path cascades to metadata."""
        file_path = FilePathModel(
            virtual_path="/test/file.txt",
            size_bytes=1024,
        )
        session.add(file_path)
        session.commit()

        path_id = file_path.path_id

        # Add metadata
        metadata = FileMetadataModel(path_id=path_id, key="author", value='"John Doe"')
        session.add(metadata)
        session.commit()

        # Delete file path
        session.delete(file_path)
        session.commit()

        # Metadata should be deleted too
        stmt = select(FileMetadataModel).where(FileMetadataModel.path_id == path_id)
        result = session.scalar(stmt)
        assert result is None


class TestFileMetadataModel:
    """Test suite for FileMetadataModel."""

    def test_create_metadata(self, session):
        """Test creating a metadata record."""
        # First create a file path
        file_path = FilePathModel(
            virtual_path="/test/file.txt",
            size_bytes=1024,
        )
        session.add(file_path)
        session.commit()

        # Create metadata
        metadata = FileMetadataModel(path_id=file_path.path_id, key="author", value='"John Doe"')
        session.add(metadata)
        session.commit()

        assert metadata.metadata_id is not None
        assert metadata.path_id == file_path.path_id
        assert metadata.key == "author"
        assert metadata.value == '"John Doe"'
        assert metadata.created_at is not None

    def test_foreign_key_constraint(self, session):
        """Test that path_id must reference existing file_path."""
        metadata = FileMetadataModel(path_id="non-existent-id", key="author", value='"John Doe"')
        session.add(metadata)

        with pytest.raises(IntegrityError):
            session.commit()

    def test_multiple_metadata_per_file(self, session):
        """Test that a file can have multiple metadata entries."""
        file_path = FilePathModel(
            virtual_path="/test/file.txt",
            size_bytes=1024,
        )
        session.add(file_path)
        session.commit()

        metadata1 = FileMetadataModel(path_id=file_path.path_id, key="author", value='"John Doe"')
        metadata2 = FileMetadataModel(path_id=file_path.path_id, key="version", value="1")
        session.add_all([metadata1, metadata2])
        session.commit()

        assert len(file_path.metadata_entries) == 2


class TestModelIndexes:
    """Test that indexes are created correctly."""

    def test_indexes_exist(self, engine):
        """Test that all expected indexes are created."""
        from sqlalchemy import inspect

        inspector = inspect(engine)

        # Check file_paths indexes
        file_paths_indexes = inspector.get_indexes("file_paths")
        index_names = [idx["name"] for idx in file_paths_indexes]
        # v0.5.0: idx_file_paths_zone_id removed - use ReBAC for multi-zone access control
        assert "idx_file_paths_content_hash" in index_names
        assert "idx_file_paths_virtual_path" in index_names

        # Check file_metadata indexes
        file_metadata_indexes = inspector.get_indexes("file_metadata")
        index_names = [idx["name"] for idx in file_metadata_indexes]
        assert "idx_file_metadata_path_id" in index_names
        assert "idx_file_metadata_key" in index_names
