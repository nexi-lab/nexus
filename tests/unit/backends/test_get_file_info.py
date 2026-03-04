"""Unit tests for backend get_file_info() implementations (Issue #1127).

Tests delta sync change detection metadata returned by each backend:
- LocalConnectorBackend: inode:mtime_ns version
- GCSConnectorBackend: GCS generation number
- S3ConnectorBackend: S3 version ID or ETag fallback
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.base.backend import FileInfo
from nexus.contracts.exceptions import NexusFileNotFoundError

# =============================================================================
# LocalConnectorBackend.get_file_info()
# =============================================================================


class TestLocalConnectorGetFileInfo:
    """Test LocalConnectorBackend.get_file_info()."""

    @pytest.fixture()
    def connector(self, tmp_path: Path):
        from nexus.backends.storage.local_connector import LocalConnectorBackend

        return LocalConnectorBackend(tmp_path)

    def test_returns_file_info_for_existing_file(self, connector, tmp_path: Path):
        """Should return FileInfo with size, mtime, and inode:mtime_ns version."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        info = connector.get_file_info("test.txt")

        assert isinstance(info, FileInfo)
        assert info.size == 11  # len("hello world")
        assert info.mtime is not None
        assert info.backend_version is not None
        # Format: "{inode}:{mtime_ns}"
        parts = info.backend_version.split(":")
        assert len(parts) == 2
        assert parts[0].isdigit()  # inode
        assert parts[1].isdigit()  # mtime_ns
        assert info.content_hash is None

    def test_raises_not_found_for_missing_file(self, connector):
        """Should raise NexusFileNotFoundError for nonexistent file."""
        with pytest.raises(NexusFileNotFoundError):
            connector.get_file_info("nonexistent.txt")

    def test_version_changes_on_content_change(self, connector, tmp_path: Path):
        """Should return different version when file content changes."""
        test_file = tmp_path / "mutable.txt"
        test_file.write_text("original")

        info1 = connector.get_file_info("mutable.txt")
        version1 = info1.backend_version

        # Modify the file (ensure mtime changes)
        import time

        time.sleep(0.01)
        test_file.write_text("modified content")

        info2 = connector.get_file_info("mutable.txt")
        version2 = info2.backend_version

        # Version should change (mtime_ns differs)
        assert version1 != version2

    def test_uses_context_backend_path(self, connector, tmp_path: Path):
        """Should use context.backend_path when available."""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "file.txt").write_text("data")

        ctx = MagicMock()
        ctx.backend_path = "sub/file.txt"

        info = connector.get_file_info("ignored_path", context=ctx)

        assert isinstance(info, FileInfo)
        assert info.size == 4

    def test_empty_file(self, connector, tmp_path: Path):
        """Should handle empty files correctly."""
        (tmp_path / "empty.txt").write_text("")

        info = connector.get_file_info("empty.txt")

        assert isinstance(info, FileInfo)
        assert info.size == 0


# =============================================================================
# GCSConnectorBackend.get_file_info()
# =============================================================================


class TestGCSConnectorGetFileInfo:
    """Test GCSConnectorBackend.get_file_info() with mocked GCS."""

    @pytest.fixture()
    def mock_bucket(self):
        bucket = MagicMock()
        return bucket

    @pytest.fixture()
    def connector(self, mock_bucket):
        from nexus.backends.storage.gcs_connector import GCSConnectorBackend

        with patch.object(GCSConnectorBackend, "__init__", lambda self, *a, **kw: None):
            c = GCSConnectorBackend.__new__(GCSConnectorBackend)
            c.bucket = mock_bucket
            c.bucket_name = "test-bucket"
            c.prefix = ""
            c._db_session = None
            c._session_factory = None
            # Set up transport mock for refactored architecture
            gcs_transport = MagicMock()
            gcs_transport.reload_blob_metadata = MagicMock()
            c._gcs_transport = gcs_transport
            c._transport = gcs_transport
            return c

    def test_returns_file_info_with_generation(self, connector):
        """Should return FileInfo with GCS generation as backend_version."""
        connector._gcs_transport.reload_blob_metadata.return_value = {
            "size": 2048,
            "updated": datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
            "generation": "1234567890",
        }

        info = connector.get_file_info("data/file.csv")

        assert isinstance(info, FileInfo)
        assert info.size == 2048
        assert info.mtime == datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        assert info.backend_version == "1234567890"
        assert info.content_hash is None

    def test_raises_not_found_for_missing_blob(self, connector):
        """Should raise NexusFileNotFoundError when blob doesn't exist."""
        connector._gcs_transport.reload_blob_metadata.side_effect = NexusFileNotFoundError(
            "missing/file.txt"
        )

        with pytest.raises(NexusFileNotFoundError):
            connector.get_file_info("missing/file.txt")

    def test_handles_none_generation(self, connector):
        """Should handle None generation gracefully."""
        connector._gcs_transport.reload_blob_metadata.return_value = {
            "size": 100,
            "updated": datetime.now(UTC),
            "generation": None,
        }

        info = connector.get_file_info("file.txt")

        assert isinstance(info, FileInfo)
        assert info.backend_version is None

    def test_handles_zero_size(self, connector):
        """Should handle blob with zero size."""
        connector._gcs_transport.reload_blob_metadata.return_value = {
            "size": 0,
            "updated": datetime.now(UTC),
            "generation": "999",
        }

        info = connector.get_file_info("empty.txt")

        assert isinstance(info, FileInfo)
        assert info.size == 0
        assert info.backend_version == "999"

    def test_uses_context_backend_path(self, connector):
        """Should use context.backend_path when available."""
        connector._gcs_transport.reload_blob_metadata.return_value = {
            "size": 512,
            "updated": datetime.now(UTC),
            "generation": "42",
        }

        ctx = MagicMock()
        ctx.backend_path = "custom/path.txt"

        connector.get_file_info("ignored", context=ctx)

        # Verify the transport was called with the context backend_path
        connector._gcs_transport.reload_blob_metadata.assert_called_once_with("custom/path.txt")


# =============================================================================
# S3ConnectorBackend.get_file_info()
# =============================================================================


class TestS3ConnectorGetFileInfo:
    """Test S3ConnectorBackend.get_file_info() with mocked S3."""

    @pytest.fixture()
    def mock_client(self):
        return MagicMock()

    @pytest.fixture()
    def connector(self, mock_client):
        from nexus.backends.storage.s3_connector import S3ConnectorBackend

        with patch.object(S3ConnectorBackend, "__init__", lambda self, *a, **kw: None):
            c = S3ConnectorBackend.__new__(S3ConnectorBackend)
            c.client = mock_client
            c.bucket_name = "test-bucket"
            c.prefix = ""
            c._db_session = None
            c._session_factory = None
            # Set up transport mock for refactored architecture
            s3_transport = MagicMock()
            s3_transport.get_object_metadata = MagicMock()
            c._s3_transport = s3_transport
            c._transport = s3_transport
            return c

    def test_returns_file_info_with_version_id(self, connector):
        """Should return FileInfo with S3 VersionId as backend_version."""
        connector._s3_transport.get_object_metadata.return_value = {
            "size": 4096,
            "last_modified": datetime(2025, 7, 1, 10, 30, 0, tzinfo=UTC),
            "version_id": "abc-123-def",
            "etag": None,
        }

        info = connector.get_file_info("data/report.pdf")

        assert isinstance(info, FileInfo)
        assert info.size == 4096
        assert info.mtime == datetime(2025, 7, 1, 10, 30, 0, tzinfo=UTC)
        assert info.backend_version == "abc-123-def"

    def test_falls_back_to_etag_when_no_version_id(self, connector):
        """Should use ETag fallback when VersionId is null."""
        connector._s3_transport.get_object_metadata.return_value = {
            "size": 1024,
            "last_modified": datetime.now(UTC),
            "version_id": "null",
            "etag": "d41d8cd98f00b204e9800998ecf8427e",
        }

        info = connector.get_file_info("file.txt")

        assert isinstance(info, FileInfo)
        assert info.backend_version == "etag:d41d8cd98f00b204e9800998ecf8427e"

    def test_falls_back_to_etag_when_version_id_missing(self, connector):
        """Should use ETag when VersionId key is absent."""
        connector._s3_transport.get_object_metadata.return_value = {
            "size": 512,
            "last_modified": datetime.now(UTC),
            "version_id": None,
            "etag": "abc123",
        }

        info = connector.get_file_info("file.txt")

        assert isinstance(info, FileInfo)
        assert info.backend_version == "etag:abc123"

    def test_raises_not_found_for_missing_object(self, connector):
        """Should raise NexusFileNotFoundError for missing object."""
        connector._s3_transport.get_object_metadata.side_effect = NexusFileNotFoundError(
            "missing.txt"
        )

        with pytest.raises(NexusFileNotFoundError):
            connector.get_file_info("missing.txt")

    def test_raises_not_found_for_no_such_key_error(self, connector):
        """Should raise NexusFileNotFoundError for NoSuchKey error code."""
        connector._s3_transport.get_object_metadata.side_effect = NexusFileNotFoundError(
            "missing.txt"
        )

        with pytest.raises(NexusFileNotFoundError):
            connector.get_file_info("missing.txt")

    def test_uses_context_backend_path(self, connector):
        """Should use context.backend_path when available."""
        connector._s3_transport.get_object_metadata.return_value = {
            "size": 256,
            "last_modified": datetime.now(UTC),
            "version_id": "v1",
            "etag": None,
        }

        ctx = MagicMock()
        ctx.backend_path = "custom/key.txt"

        connector.get_file_info("ignored", context=ctx)

        # Verify the transport was called with the context backend_path
        connector._s3_transport.get_object_metadata.assert_called_once_with("custom/key.txt")
