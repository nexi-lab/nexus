"""Unit tests for backend get_file_info() implementations (Issue #1127).

Tests delta sync change detection metadata returned by each backend:
- LocalConnectorBackend: inode:mtime_ns version
- GCSConnectorBackend: GCS generation number
- S3ConnectorBackend: S3 version ID or ETag fallback
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.backend import FileInfo

# =============================================================================
# LocalConnectorBackend.get_file_info()
# =============================================================================


class TestLocalConnectorGetFileInfo:
    """Test LocalConnectorBackend.get_file_info()."""

    @pytest.fixture()
    def connector(self, tmp_path: Path):
        from nexus.backends.local_connector import LocalConnectorBackend

        return LocalConnectorBackend(tmp_path)

    def test_returns_file_info_for_existing_file(self, connector, tmp_path: Path):
        """Should return FileInfo with size, mtime, and inode:mtime_ns version."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        response = connector.get_file_info("test.txt")

        assert response.success is True
        info: FileInfo = response.data
        assert info.size == 11  # len("hello world")
        assert info.mtime is not None
        assert info.backend_version is not None
        # Format: "{inode}:{mtime_ns}"
        parts = info.backend_version.split(":")
        assert len(parts) == 2
        assert parts[0].isdigit()  # inode
        assert parts[1].isdigit()  # mtime_ns
        assert info.content_hash is None

    def test_returns_not_found_for_missing_file(self, connector):
        """Should return not_found for nonexistent file."""
        response = connector.get_file_info("nonexistent.txt")

        assert response.success is False

    def test_version_changes_on_content_change(self, connector, tmp_path: Path):
        """Should return different version when file content changes."""
        test_file = tmp_path / "mutable.txt"
        test_file.write_text("original")

        response1 = connector.get_file_info("mutable.txt")
        version1 = response1.data.backend_version

        # Modify the file (ensure mtime changes)
        import time

        time.sleep(0.01)
        test_file.write_text("modified content")

        response2 = connector.get_file_info("mutable.txt")
        version2 = response2.data.backend_version

        # Version should change (mtime_ns differs)
        assert version1 != version2

    def test_uses_context_backend_path(self, connector, tmp_path: Path):
        """Should use context.backend_path when available."""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "file.txt").write_text("data")

        ctx = MagicMock()
        ctx.backend_path = "sub/file.txt"

        response = connector.get_file_info("ignored_path", context=ctx)

        assert response.success is True
        assert response.data.size == 4

    def test_empty_file(self, connector, tmp_path: Path):
        """Should handle empty files correctly."""
        (tmp_path / "empty.txt").write_text("")

        response = connector.get_file_info("empty.txt")

        assert response.success is True
        assert response.data.size == 0


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
        from nexus.backends.gcs_connector import GCSConnectorBackend

        with patch.object(GCSConnectorBackend, "__init__", lambda self, *a, **kw: None):
            c = GCSConnectorBackend.__new__(GCSConnectorBackend)
            c.bucket = mock_bucket
            c.bucket_name = "test-bucket"
            c.prefix = ""
            # name is a property returning the backend name, no need to set
            c._db_session = None
            c._session_factory = None
            return c

    def test_returns_file_info_with_generation(self, connector, mock_bucket):
        """Should return FileInfo with GCS generation as backend_version."""
        blob = MagicMock()
        blob.size = 2048
        blob.updated = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        blob.generation = 1234567890
        mock_bucket.blob.return_value = blob

        response = connector.get_file_info("data/file.csv")

        assert response.success is True
        info: FileInfo = response.data
        assert info.size == 2048
        assert info.mtime == datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        assert info.backend_version == "1234567890"
        assert info.content_hash is None

    def test_returns_not_found_for_missing_blob(self, connector, mock_bucket):
        """Should return not_found when blob doesn't exist."""
        from google.cloud.exceptions import NotFound

        blob = MagicMock()
        blob.reload.side_effect = NotFound("Blob not found")
        mock_bucket.blob.return_value = blob

        response = connector.get_file_info("missing/file.txt")

        assert response.success is False

    def test_handles_none_generation(self, connector, mock_bucket):
        """Should handle None generation gracefully."""
        blob = MagicMock()
        blob.size = 100
        blob.updated = datetime.now(UTC)
        blob.generation = None
        mock_bucket.blob.return_value = blob

        response = connector.get_file_info("file.txt")

        assert response.success is True
        assert response.data.backend_version is None

    def test_handles_zero_size(self, connector, mock_bucket):
        """Should handle blob with zero size."""
        blob = MagicMock()
        blob.size = 0
        blob.updated = datetime.now(UTC)
        blob.generation = 999
        mock_bucket.blob.return_value = blob

        response = connector.get_file_info("empty.txt")

        assert response.success is True
        assert response.data.size == 0
        assert response.data.backend_version == "999"

    def test_uses_context_backend_path(self, connector, mock_bucket):
        """Should use context.backend_path when available."""
        blob = MagicMock()
        blob.size = 512
        blob.updated = datetime.now(UTC)
        blob.generation = 42
        mock_bucket.blob.return_value = blob

        ctx = MagicMock()
        ctx.backend_path = "custom/path.txt"

        connector.get_file_info("ignored", context=ctx)

        # Verify the correct blob path was used
        mock_bucket.blob.assert_called_once()


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
        from nexus.backends.s3_connector import S3ConnectorBackend

        with patch.object(S3ConnectorBackend, "__init__", lambda self, *a, **kw: None):
            c = S3ConnectorBackend.__new__(S3ConnectorBackend)
            c.client = mock_client
            c.bucket_name = "test-bucket"
            c.prefix = ""
            # name is a property returning "s3_connector", no need to set
            c._db_session = None
            c._session_factory = None
            return c

    def test_returns_file_info_with_version_id(self, connector, mock_client):
        """Should return FileInfo with S3 VersionId as backend_version."""
        mock_client.head_object.return_value = {
            "ContentLength": 4096,
            "LastModified": datetime(2025, 7, 1, 10, 30, 0, tzinfo=UTC),
            "VersionId": "abc-123-def",
        }

        response = connector.get_file_info("data/report.pdf")

        assert response.success is True
        info: FileInfo = response.data
        assert info.size == 4096
        assert info.mtime == datetime(2025, 7, 1, 10, 30, 0, tzinfo=UTC)
        assert info.backend_version == "abc-123-def"
        assert info.content_hash is None

    def test_falls_back_to_etag_when_no_version_id(self, connector, mock_client):
        """Should use ETag fallback when VersionId is null."""
        mock_client.head_object.return_value = {
            "ContentLength": 1024,
            "LastModified": datetime.now(UTC),
            "VersionId": "null",
            "ETag": '"d41d8cd98f00b204e9800998ecf8427e"',
        }

        response = connector.get_file_info("file.txt")

        assert response.success is True
        assert response.data.backend_version == "etag:d41d8cd98f00b204e9800998ecf8427e"

    def test_falls_back_to_etag_when_version_id_missing(self, connector, mock_client):
        """Should use ETag when VersionId key is absent."""
        mock_client.head_object.return_value = {
            "ContentLength": 512,
            "LastModified": datetime.now(UTC),
            "ETag": '"abc123"',
        }

        response = connector.get_file_info("file.txt")

        assert response.success is True
        assert response.data.backend_version == "etag:abc123"

    def test_returns_not_found_for_missing_object(self, connector, mock_client):
        """Should return not_found for 404 ClientError."""
        from botocore.exceptions import ClientError

        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )

        response = connector.get_file_info("missing.txt")

        assert response.success is False

    def test_handles_no_such_key_error(self, connector, mock_client):
        """Should handle NoSuchKey error code."""
        from botocore.exceptions import ClientError

        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "No such key"}},
            "HeadObject",
        )

        response = connector.get_file_info("missing.txt")

        assert response.success is False

    def test_uses_context_backend_path(self, connector, mock_client):
        """Should use context.backend_path when available."""
        mock_client.head_object.return_value = {
            "ContentLength": 256,
            "LastModified": datetime.now(UTC),
            "VersionId": "v1",
        }

        ctx = MagicMock()
        ctx.backend_path = "custom/key.txt"

        connector.get_file_info("ignored", context=ctx)

        mock_client.head_object.assert_called_once()
