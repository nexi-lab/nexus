"""Mocked unit tests for GCS backend — Issue #2960 (GCS zero tests).

Tests GCS-specific error handling, CAS operations, and metadata management
using mocked google.cloud.storage client. Does not require real GCS credentials.
"""

import json
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError


def _make_gcs_backend(
    bucket_name: str = "test-bucket",
) -> tuple["GCSBackend", MagicMock, MagicMock]:
    """Create a GCSBackend with mocked GCS client, bypassing __init__ validation."""
    from nexus.backends.gcs import GCSBackend

    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.exists.return_value = True

    # Bypass __init__ by creating instance directly
    backend = object.__new__(GCSBackend)
    backend.client = mock_client
    backend.bucket = mock_bucket
    backend.bucket_name = bucket_name
    backend._operation_timeout = 60.0
    backend._upload_timeout = 300.0

    return backend, mock_client, mock_bucket


class TestGCSHashToPath:
    """Test CAS path generation."""

    def test_valid_hash(self) -> None:
        backend, _, _ = _make_gcs_backend()
        path = backend._hash_to_path("abcdef1234567890")
        assert path == "cas/ab/cd/abcdef1234567890"

    def test_short_hash_raises(self) -> None:
        backend, _, _ = _make_gcs_backend()
        with pytest.raises(ValueError, match="Invalid hash length"):
            backend._hash_to_path("abc")

    def test_meta_path(self) -> None:
        backend, _, _ = _make_gcs_backend()
        path = backend._get_meta_path("abcdef1234567890")
        assert path == "cas/ab/cd/abcdef1234567890.meta"


class TestGCSReadMetadata:
    """Test metadata read operations."""

    def test_read_existing_metadata(self) -> None:
        backend, _, mock_bucket = _make_gcs_backend()
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = json.dumps({"ref_count": 3, "size": 1024})
        mock_bucket.blob.return_value = mock_blob

        metadata = backend._read_metadata("abcdef1234567890")
        assert metadata["ref_count"] == 3
        assert metadata["size"] == 1024

    def test_read_missing_metadata_returns_defaults(self) -> None:
        backend, _, mock_bucket = _make_gcs_backend()
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket.blob.return_value = mock_blob

        metadata = backend._read_metadata("abcdef1234567890")
        assert metadata == {"ref_count": 0, "size": 0}


class TestGCSWriteContent:
    """Test CAS write operations."""

    @patch("nexus.backends.gcs.hash_content")
    def test_write_new_content(self, mock_hash: MagicMock) -> None:
        mock_hash.return_value = "abcdef1234567890"
        backend, _, mock_bucket = _make_gcs_backend()

        content_blob = MagicMock()
        content_blob.exists.return_value = False
        meta_blob = MagicMock()
        meta_blob.exists.return_value = False

        def blob_router(path: str) -> MagicMock:
            if path.endswith(".meta"):
                return meta_blob
            return content_blob

        mock_bucket.blob.side_effect = blob_router

        result = backend.write_content(b"hello world")
        assert result == "abcdef1234567890"
        content_blob.upload_from_string.assert_called_once_with(
            b"hello world", timeout=60.0
        )

    @patch("nexus.backends.gcs.hash_content")
    def test_write_existing_content_increments_ref_count(self, mock_hash: MagicMock) -> None:
        mock_hash.return_value = "abcdef1234567890"
        backend, _, mock_bucket = _make_gcs_backend()

        content_blob = MagicMock()
        content_blob.exists.return_value = True  # Content already exists

        meta_blob = MagicMock()
        meta_blob.exists.return_value = True
        meta_blob.download_as_text.return_value = json.dumps({"ref_count": 2, "size": 11})

        def blob_router(path: str) -> MagicMock:
            if path.endswith(".meta"):
                return meta_blob
            return content_blob

        mock_bucket.blob.side_effect = blob_router

        result = backend.write_content(b"hello world")
        assert result == "abcdef1234567890"
        # Content blob should NOT be uploaded again
        content_blob.upload_from_string.assert_not_called()
        # Metadata should be updated with incremented ref_count
        meta_blob.upload_from_string.assert_called_once()
        uploaded = json.loads(meta_blob.upload_from_string.call_args[0][0])
        assert uploaded["ref_count"] == 3


class TestGCSReadContent:
    """Test CAS read operations."""

    @patch("nexus.backends.gcs.hash_content")
    def test_read_existing_content(self, mock_hash: MagicMock) -> None:
        mock_hash.return_value = "abcdef1234567890"
        backend, _, mock_bucket = _make_gcs_backend()

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = b"hello world"
        mock_bucket.blob.return_value = mock_blob

        content = backend.read_content("abcdef1234567890")
        assert content == b"hello world"

    def test_read_missing_content_raises(self) -> None:
        backend, _, mock_bucket = _make_gcs_backend()

        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket.blob.return_value = mock_blob

        with pytest.raises(NexusFileNotFoundError):
            backend.read_content("nonexistent_hash")

    @patch("nexus.backends.gcs.hash_content")
    def test_read_content_hash_mismatch_raises(self, mock_hash: MagicMock) -> None:
        """Regression: verify hash integrity check on read."""
        mock_hash.return_value = "different_hash"
        backend, _, mock_bucket = _make_gcs_backend()

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = b"corrupted"
        mock_bucket.blob.return_value = mock_blob

        with pytest.raises(BackendError, match="Content hash mismatch"):
            backend.read_content("expected_hash")


class TestGCSWriteMetadata:
    """Test metadata write operations."""

    def test_write_metadata(self) -> None:
        backend, _, mock_bucket = _make_gcs_backend()
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob

        backend._write_metadata("abcdef1234567890", {"ref_count": 5, "size": 2048})

        mock_blob.upload_from_string.assert_called_once()
        uploaded = json.loads(mock_blob.upload_from_string.call_args[0][0])
        assert uploaded["ref_count"] == 5
        assert uploaded["size"] == 2048

    def test_write_metadata_error_raises_backend_error(self) -> None:
        backend, _, mock_bucket = _make_gcs_backend()
        mock_blob = MagicMock()
        mock_blob.upload_from_string.side_effect = Exception("GCS timeout")
        mock_bucket.blob.return_value = mock_blob

        with pytest.raises(BackendError, match="Failed to write metadata"):
            backend._write_metadata("abcdef1234567890", {"ref_count": 1})


class TestGCSInitialization:
    """Test backend initialization and error handling."""

    @patch("nexus.backends.gcs.storage")
    def test_nonexistent_bucket_raises(self, mock_storage: MagicMock) -> None:
        mock_client = MagicMock()
        mock_storage.Client.return_value = mock_client
        mock_bucket = MagicMock()
        mock_bucket.exists.return_value = False
        mock_client.bucket.return_value = mock_bucket

        from nexus.backends.gcs import GCSBackend

        with pytest.raises(BackendError, match="does not exist"):
            GCSBackend(bucket_name="nonexistent-bucket")

    @patch("nexus.backends.gcs.storage")
    def test_connection_error_raises(self, mock_storage: MagicMock) -> None:
        mock_storage.Client.side_effect = Exception("Connection refused")

        from nexus.backends.gcs import GCSBackend

        with pytest.raises(BackendError, match="Failed to initialize"):
            GCSBackend(bucket_name="test-bucket")
