"""Unit tests for SQLAlchemy models."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from nexus.storage.models import Base, ContentChunkModel, FileMetadataModel, FilePathModel


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
            backend_id="backend-123",
            physical_path="/data/file.txt",
            size_bytes=1024,
            content_hash="abc123",
            file_type="text/plain",
        )
        session.add(file_path)
        session.commit()

        assert file_path.path_id is not None
        assert file_path.tenant_id is not None
        assert file_path.virtual_path == "/test/file.txt"
        assert file_path.created_at is not None
        assert file_path.updated_at is not None

    def test_unique_constraint_tenant_virtual_path(self, session):
        """Test that tenant_id + virtual_path must be unique."""
        tenant_id = "tenant-123"

        file1 = FilePathModel(
            tenant_id=tenant_id,
            virtual_path="/test/file.txt",
            backend_id="backend-123",
            physical_path="/data/file1.txt",
            size_bytes=1024,
        )
        session.add(file1)
        session.commit()

        # Try to create duplicate
        file2 = FilePathModel(
            tenant_id=tenant_id,
            virtual_path="/test/file.txt",
            backend_id="backend-456",
            physical_path="/data/file2.txt",
            size_bytes=2048,
        )
        session.add(file2)

        with pytest.raises(IntegrityError):
            session.commit()

    def test_different_tenants_can_have_same_path(self, session):
        """Test that different tenants can have the same virtual path."""
        file1 = FilePathModel(
            tenant_id="tenant-1",
            virtual_path="/test/file.txt",
            backend_id="backend-123",
            physical_path="/data/file1.txt",
            size_bytes=1024,
        )
        file2 = FilePathModel(
            tenant_id="tenant-2",
            virtual_path="/test/file.txt",
            backend_id="backend-456",
            physical_path="/data/file2.txt",
            size_bytes=2048,
        )

        session.add_all([file1, file2])
        session.commit()

        assert file1.path_id != file2.path_id

    def test_soft_delete(self, session):
        """Test soft delete functionality."""
        file_path = FilePathModel(
            virtual_path="/test/file.txt",
            backend_id="backend-123",
            physical_path="/data/file.txt",
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
            backend_id="backend-123",
            physical_path="/data/file.txt",
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
            backend_id="backend-123",
            physical_path="/data/file.txt",
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
            backend_id="backend-123",
            physical_path="/data/file.txt",
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
            backend_id="backend-123",
            physical_path="/data/file.txt",
            size_bytes=1024,
        )
        session.add(file_path)
        session.commit()

        metadata1 = FileMetadataModel(path_id=file_path.path_id, key="author", value='"John Doe"')
        metadata2 = FileMetadataModel(path_id=file_path.path_id, key="version", value="1")
        session.add_all([metadata1, metadata2])
        session.commit()

        assert len(file_path.metadata_entries) == 2


class TestContentChunkModel:
    """Test suite for ContentChunkModel."""

    def test_create_content_chunk(self, session):
        """Test creating a content chunk record."""
        chunk = ContentChunkModel(
            content_hash="abc123def456",
            size_bytes=4096,
            storage_path="/chunks/abc123def456",
            ref_count=1,
        )
        session.add(chunk)
        session.commit()

        assert chunk.chunk_id is not None
        assert chunk.content_hash == "abc123def456"
        assert chunk.size_bytes == 4096
        assert chunk.ref_count == 1
        assert chunk.created_at is not None

    def test_unique_content_hash(self, session):
        """Test that content_hash must be unique."""
        chunk1 = ContentChunkModel(
            content_hash="abc123",
            size_bytes=4096,
            storage_path="/chunks/abc123-1",
            ref_count=1,
        )
        session.add(chunk1)
        session.commit()

        # Try to create duplicate
        chunk2 = ContentChunkModel(
            content_hash="abc123",
            size_bytes=8192,
            storage_path="/chunks/abc123-2",
            ref_count=2,
        )
        session.add(chunk2)

        with pytest.raises(IntegrityError):
            session.commit()

    def test_ref_count_increment(self, session):
        """Test incrementing reference count."""
        chunk = ContentChunkModel(
            content_hash="abc123",
            size_bytes=4096,
            storage_path="/chunks/abc123",
            ref_count=1,
        )
        session.add(chunk)
        session.commit()

        # Increment ref count
        chunk.ref_count += 1
        session.commit()

        assert chunk.ref_count == 2

    def test_last_accessed_at(self, session):
        """Test updating last accessed timestamp."""
        chunk = ContentChunkModel(
            content_hash="abc123",
            size_bytes=4096,
            storage_path="/chunks/abc123",
            ref_count=1,
        )
        session.add(chunk)
        session.commit()

        assert chunk.last_accessed_at is None

        # Update last accessed
        chunk.last_accessed_at = datetime.now(UTC)
        session.commit()

        assert chunk.last_accessed_at is not None


class TestModelIndexes:
    """Test that indexes are created correctly."""

    def test_indexes_exist(self, engine):
        """Test that all expected indexes are created."""
        from sqlalchemy import inspect

        inspector = inspect(engine)

        # Check file_paths indexes
        file_paths_indexes = inspector.get_indexes("file_paths")
        index_names = [idx["name"] for idx in file_paths_indexes]
        assert "idx_file_paths_tenant_id" in index_names
        assert "idx_file_paths_backend_id" in index_names
        assert "idx_file_paths_content_hash" in index_names
        assert "idx_file_paths_virtual_path" in index_names

        # Check file_metadata indexes
        file_metadata_indexes = inspector.get_indexes("file_metadata")
        index_names = [idx["name"] for idx in file_metadata_indexes]
        assert "idx_file_metadata_path_id" in index_names
        assert "idx_file_metadata_key" in index_names

        # Check content_chunks indexes
        content_chunks_indexes = inspector.get_indexes("content_chunks")
        index_names = [idx["name"] for idx in content_chunks_indexes]
        assert "idx_content_chunks_hash" in index_names
        assert "idx_content_chunks_ref_count" in index_names
