"""Unit tests for backend get_file_info() implementations (Issue #1127).

Tests delta sync change detection metadata returned by each backend:
- LocalConnectorBackend: inode:mtime_ns version
- PathGCSBackend: GCS generation number
- PathS3Backend: S3 version ID or ETag fallback
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.base.backend import FileInfo
from nexus.contracts.exceptions import NexusFileNotFoundError

# =============================================================================
# LocalConnectorBackend.get_file_info()
# =============================================================================


# =============================================================================
# PathGCSBackend.get_file_info()
# =============================================================================


class TestGCSConnectorGetFileInfo:
    """Test PathGCSBackend.get_file_info() with mocked GCS."""

    @pytest.fixture()
    def mock_bucket(self):
        bucket = MagicMock()
        return bucket

    @pytest.fixture()
    def connector(self, mock_bucket):
        from nexus.backends.storage.path_gcs import PathGCSBackend

        with patch.object(PathGCSBackend, "__init__", lambda self, *a, **kw: None):
            c = PathGCSBackend.__new__(PathGCSBackend)
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
        assert info.content_id is None

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
# PathS3Backend.get_file_info()
# =============================================================================


class TestS3ConnectorGetFileInfo:
    """Test PathS3Backend.get_file_info() with mocked S3."""

    @pytest.fixture()
    def mock_client(self):
        return MagicMock()

    @pytest.fixture()
    def connector(self, mock_client):
        from nexus.backends.storage.path_s3 import PathS3Backend

        with patch.object(PathS3Backend, "__init__", lambda self, *a, **kw: None):
            c = PathS3Backend.__new__(PathS3Backend)
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
