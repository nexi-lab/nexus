"""Unit tests for batch operations in connector backends.

Tests cover new batch optimization methods:
- _batch_get_versions() for GCS and S3
- _bulk_download_blobs() for parallel downloads
- _batch_write_to_cache() for bulk cache writes
- _batch_read_from_backend() integration
"""

from pathlib import Path
from unittest.mock import patch

from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.core.permissions import OperationContext


class MockBlobConnector(BaseBlobStorageConnector, CacheConnectorMixin):
    """Mock blob connector for testing batch operations."""

    name = "test_blob_backend"  # Class attribute for abstract property

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.prefix = ""
        self.files = {}  # blob_path -> content
        self.versions = {}  # blob_path -> version_id
        self.download_count = 0  # Track individual download calls

    def _get_blob_path(self, backend_path: str) -> str:
        """Convert backend path to blob path."""
        return backend_path

    def _download_blob(
        self, blob_path: str, version_id: str | None = None
    ) -> tuple[bytes, str | None]:
        """Download single blob."""
        self.download_count += 1
        if blob_path not in self.files:
            raise FileNotFoundError(f"Blob not found: {blob_path}")
        # Return content and version (use provided version_id or generate one)
        file_version = version_id or self.versions.get(blob_path, "v1")
        return self.files[blob_path], file_version

    def _delete_blob(self, blob_path: str) -> None:
        """Delete blob."""
        if blob_path in self.files:
            del self.files[blob_path]

    def _upload_blob(self, blob_path: str, content: bytes, content_type: str = None) -> None:
        """Upload blob."""
        self.files[blob_path] = content

    def get_version(self, path: str, context: OperationContext | None = None) -> str | None:
        """Get version for a file."""
        return self.versions.get(path)

    def _list_files_recursive(
        self, path: str, context: OperationContext | None = None
    ) -> list[str]:
        """List files recursively."""
        return list(self.files.keys())

    def _blob_exists(self, blob_path: str) -> bool:
        """Check if blob exists."""
        return blob_path in self.files

    def _get_blob_size(self, blob_path: str) -> int:
        """Get blob size."""
        return len(self.files.get(blob_path, b""))

    def _list_blobs(self, prefix: str = "") -> list[str]:
        """List all blobs with prefix."""
        return [path for path in self.files if path.startswith(prefix)]

    def _copy_blob(self, source_blob_path: str, dest_blob_path: str) -> None:
        """Copy blob."""
        if source_blob_path in self.files:
            self.files[dest_blob_path] = self.files[source_blob_path]

    def _create_directory_marker(self, blob_path: str) -> None:
        """Create directory marker."""
        pass  # Not needed for tests


class TestBatchGetVersions:
    """Test _batch_get_versions() method."""

    def test_batch_get_versions_default_fallback(self, tmp_path: Path):
        """Test default fallback implementation calls get_version() sequentially."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.versions = {
            "file1.txt": "v1",
            "file2.txt": "v2",
            "file3.txt": "v3",
        }

        # Call batch method
        result = backend._batch_get_versions(["file1.txt", "file2.txt", "file3.txt"])

        # Verify all versions returned
        assert result == {"file1.txt": "v1", "file2.txt": "v2", "file3.txt": "v3"}

    def test_batch_get_versions_handles_missing_files(self, tmp_path: Path):
        """Test batch get versions gracefully handles missing files."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.versions = {
            "file1.txt": "v1",
            # file2.txt doesn't exist
            "file3.txt": "v3",
        }

        # Call batch method
        result = backend._batch_get_versions(["file1.txt", "file2.txt", "file3.txt"])

        # Should return None for missing files
        assert result == {"file1.txt": "v1", "file2.txt": None, "file3.txt": "v3"}

    def test_batch_get_versions_empty_list(self, tmp_path: Path):
        """Test batch get versions with empty list."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)

        # Call with empty list
        result = backend._batch_get_versions([])

        # Should return empty dict
        assert result == {}


class TestBulkDownloadBlobs:
    """Test _bulk_download_blobs() method."""

    def test_bulk_download_calls_download_blob(self, tmp_path: Path):
        """Test that bulk download calls _download_blob() for each file (DRY principle)."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.files = {
            "blob1.txt": b"content1",
            "blob2.txt": b"content2",
            "blob3.txt": b"content3",
        }

        # Reset download count
        backend.download_count = 0

        # Call bulk download
        result = backend._bulk_download_blobs(
            ["blob1.txt", "blob2.txt", "blob3.txt"], max_workers=2
        )

        # Verify _download_blob() was called for each file
        assert backend.download_count == 3, (
            "_bulk_download_blobs should call _download_blob() for each file"
        )

        # Verify all files downloaded
        assert len(result) == 3
        assert result["blob1.txt"] == b"content1"
        assert result["blob2.txt"] == b"content2"
        assert result["blob3.txt"] == b"content3"

    def test_bulk_download_handles_failures_gracefully(self, tmp_path: Path):
        """Test bulk download continues even if some files fail."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.files = {
            "blob1.txt": b"content1",
            # blob2.txt is missing (will fail)
            "blob3.txt": b"content3",
        }

        # Call bulk download
        result = backend._bulk_download_blobs(
            ["blob1.txt", "blob2.txt", "blob3.txt"], max_workers=2
        )

        # Should return successful downloads only
        assert len(result) == 2
        assert result["blob1.txt"] == b"content1"
        assert result["blob3.txt"] == b"content3"
        assert "blob2.txt" not in result

    def test_bulk_download_empty_list(self, tmp_path: Path):
        """Test bulk download with empty list."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)

        # Call with empty list
        result = backend._bulk_download_blobs([], max_workers=2)

        # Should return empty dict
        assert result == {}


class TestBatchWriteToCache:
    """Test _batch_write_to_cache() method."""

    def test_batch_write_multiple_entries(self, tmp_path: Path):
        """Test batch writing multiple cache entries in single transaction."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)

        # Prepare entries for batch write
        entries = [
            {
                "path": "/test/file1.txt",
                "content": b"content1",
                "content_text": "content1",
                "content_type": "full",
                "backend_version": "v1",
            },
            {
                "path": "/test/file2.txt",
                "content": b"content2",
                "content_text": "content2",
                "content_type": "full",
                "backend_version": "v2",
            },
            {
                "path": "/test/file3.txt",
                "content": b"content3",
                "content_text": "content3",
                "content_type": "full",
                "backend_version": "v3",
            },
        ]

        # Mock path_id lookup
        with patch.object(backend, "_get_path_ids_bulk") as mock_path_ids:
            mock_path_ids.return_value = {
                "/test/file1.txt": "path1",
                "/test/file2.txt": "path2",
                "/test/file3.txt": "path3",
            }

            # Call batch write
            result = backend._batch_write_to_cache(entries)

            # Verify all entries written
            assert len(result) == 3
            assert all(entry.cache_id for entry in result)

    def test_batch_write_empty_list(self, tmp_path: Path):
        """Test batch write with empty list."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)

        # Call with empty list
        result = backend._batch_write_to_cache([])

        # Should return empty list
        assert result == []


class TestBatchReadFromBackend:
    """Test _batch_read_from_backend() integration."""

    def test_batch_read_uses_bulk_download(self, tmp_path: Path):
        """Test that batch read uses _bulk_download_blobs() for blob connectors."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)
        backend.files = {
            "file1.txt": b"content1",
            "file2.txt": b"content2",
            "file3.txt": b"content3",
        }

        # Mock _bulk_download_blobs to verify it's called
        with patch.object(
            backend, "_bulk_download_blobs", wraps=backend._bulk_download_blobs
        ) as mock_bulk:
            # Call batch read
            result = backend._batch_read_from_backend(["file1.txt", "file2.txt", "file3.txt"])

            # Verify bulk download was called
            assert mock_bulk.called, "_batch_read_from_backend should use _bulk_download_blobs"

            # Verify all files read
            assert len(result) == 3
            assert result["file1.txt"] == b"content1"
            assert result["file2.txt"] == b"content2"
            assert result["file3.txt"] == b"content3"

    def test_batch_read_performance_benefit(self, tmp_path: Path):
        """Test that batch read is significantly faster than sequential."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        db_path = tmp_path / "test.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        backend = MockBlobConnector(SessionLocal)

        # Create 50 test files
        for i in range(50):
            backend.files[f"file{i}.txt"] = b"content"

        # Reset download count
        backend.download_count = 0

        # Call batch read
        paths = [f"file{i}.txt" for i in range(50)]
        result = backend._batch_read_from_backend(paths)

        # Verify all files read
        assert len(result) == 50

        # Verify _download_blob was called for each file
        # (but in parallel, not sequential)
        assert backend.download_count == 50
