"""Unit tests for GCS backend."""

from unittest.mock import Mock, patch

import pytest

from nexus.backends.gcs import GCSBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content


@pytest.fixture
def mock_storage_client() -> Mock:
    """Create a mock GCS storage client."""
    with patch("nexus.backends.gcs.storage") as mock_storage:
        mock_client = Mock()
        mock_bucket = Mock()
        mock_bucket.exists.return_value = True
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client
        yield mock_storage


@pytest.fixture
def gcs_backend(mock_storage_client: Mock) -> GCSBackend:
    """Create a GCS backend instance with mocked client."""
    return GCSBackend(bucket_name="test-bucket", project_id="test-project")


class TestGCSBackendInitialization:
    """Test GCS backend initialization."""

    def test_init_with_default_credentials(self, mock_storage_client: Mock) -> None:
        """Test initialization with Application Default Credentials."""
        backend = GCSBackend(bucket_name="test-bucket")

        # Should use default Client() constructor
        mock_storage_client.Client.assert_called_once_with(project=None)
        assert backend.bucket_name == "test-bucket"

    def test_init_with_project_id(self, mock_storage_client: Mock) -> None:
        """Test initialization with project ID."""
        backend = GCSBackend(bucket_name="test-bucket", project_id="my-project")

        mock_storage_client.Client.assert_called_once_with(project="my-project")
        assert backend.bucket_name == "test-bucket"

    def test_init_with_credentials_file(self, mock_storage_client: Mock) -> None:
        """Test initialization with explicit credentials file."""
        backend = GCSBackend(
            bucket_name="test-bucket",
            project_id="my-project",
            credentials_path="/path/to/creds.json",
        )

        # Should use from_service_account_json
        mock_storage_client.Client.from_service_account_json.assert_called_once_with(
            "/path/to/creds.json", project="my-project"
        )
        assert backend.bucket_name == "test-bucket"

    def test_init_bucket_not_exists(self, mock_storage_client: Mock) -> None:
        """Test initialization fails when bucket doesn't exist."""
        mock_client = Mock()
        mock_bucket = Mock()
        mock_bucket.exists.return_value = False
        mock_client.bucket.return_value = mock_bucket
        mock_storage_client.Client.return_value = mock_client

        with pytest.raises(BackendError) as exc_info:
            GCSBackend(bucket_name="nonexistent-bucket")

        assert "does not exist" in str(exc_info.value)
        assert "nonexistent-bucket" in str(exc_info.value)

    def test_init_connection_error(self, mock_storage_client: Mock) -> None:
        """Test initialization fails on connection error."""
        mock_storage_client.Client.side_effect = Exception("Connection failed")

        with pytest.raises(BackendError) as exc_info:
            GCSBackend(bucket_name="test-bucket")

        assert "Failed to initialize GCS backend" in str(exc_info.value)


class TestContentOperations:
    """Test content-addressable storage operations."""

    def test_write_content_new(self, gcs_backend: GCSBackend) -> None:
        """Test writing new content."""
        test_content = b"Hello, GCS!"
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        content_hash = gcs_backend.write_content(test_content)

        # Should be SHA-256 hash
        assert len(content_hash) == 64
        # Verify it's a valid hex string
        int(content_hash, 16)

        # Should write content and metadata (upload_from_string called twice)
        assert mock_blob.upload_from_string.call_count == 2
        # First call should be content
        first_call = mock_blob.upload_from_string.call_args_list[0]
        assert first_call[0][0] == test_content
        # Second call should be metadata (JSON)
        second_call = mock_blob.upload_from_string.call_args_list[1]
        assert '"ref_count": 1' in second_call[0][0]

        # Should create blobs for content + metadata
        assert gcs_backend.bucket.blob.call_count >= 2

    def test_write_content_duplicate(self, gcs_backend: GCSBackend) -> None:
        """Test writing duplicate content increments ref count."""
        test_content = b"Hello, GCS!"
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock metadata read
        mock_meta_blob = Mock()
        mock_meta_blob.exists.return_value = True
        mock_meta_blob.download_as_text.return_value = '{"ref_count": 1, "size": 11}'

        def blob_side_effect(path: str) -> Mock:
            if path.endswith(".meta"):
                return mock_meta_blob
            return mock_blob

        gcs_backend.bucket.blob.side_effect = blob_side_effect

        gcs_backend.write_content(test_content)

        # Should not write content again (exists)
        assert mock_blob.upload_from_string.call_count == 0

        # Should update metadata with incremented ref count
        assert mock_meta_blob.upload_from_string.call_count == 1

    def test_read_content_success(self, gcs_backend: GCSBackend) -> None:
        """Test reading content successfully."""
        test_content = b"Hello, GCS!"

        # Compute the actual hash for this content (using BLAKE3)
        expected_hash = hash_content(test_content)

        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = test_content
        gcs_backend.bucket.blob.return_value = mock_blob

        result = gcs_backend.read_content(expected_hash)

        assert result == test_content
        mock_blob.download_as_bytes.assert_called_once_with(timeout=60)

    def test_read_content_not_found(self, gcs_backend: GCSBackend) -> None:
        """Test reading non-existent content."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        with pytest.raises(NexusFileNotFoundError):
            gcs_backend.read_content("nonexistent_hash")

    def test_read_content_hash_mismatch(self, gcs_backend: GCSBackend) -> None:
        """Test reading content with hash mismatch."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = b"corrupted data"
        gcs_backend.bucket.blob.return_value = mock_blob

        with pytest.raises(BackendError) as exc_info:
            gcs_backend.read_content("expected_hash")

        assert "hash mismatch" in str(exc_info.value).lower()

    def test_content_exists_true(self, gcs_backend: GCSBackend) -> None:
        """Test content_exists returns True for existing content."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        result = gcs_backend.content_exists("some_hash")

        assert result is True

    def test_content_exists_false(self, gcs_backend: GCSBackend) -> None:
        """Test content_exists returns False for non-existent content."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        result = gcs_backend.content_exists("some_hash")

        assert result is False

    def test_get_content_size(self, gcs_backend: GCSBackend) -> None:
        """Test getting content size."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.size = 1024
        gcs_backend.bucket.blob.return_value = mock_blob

        size = gcs_backend.get_content_size("some_hash")

        assert size == 1024
        mock_blob.reload.assert_called_once()

    def test_get_content_size_not_found(self, gcs_backend: GCSBackend) -> None:
        """Test getting size of non-existent content."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        with pytest.raises(NexusFileNotFoundError):
            gcs_backend.get_content_size("nonexistent_hash")

    def test_get_ref_count(self, gcs_backend: GCSBackend) -> None:
        """Test getting reference count."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock metadata read
        mock_meta_blob = Mock()
        mock_meta_blob.exists.return_value = True
        mock_meta_blob.download_as_text.return_value = '{"ref_count": 3, "size": 100}'

        def blob_side_effect(path: str) -> Mock:
            if path.endswith(".meta"):
                return mock_meta_blob
            return mock_blob

        gcs_backend.bucket.blob.side_effect = blob_side_effect

        ref_count = gcs_backend.get_ref_count("some_hash")

        assert ref_count == 3

    def test_delete_content_decrement_ref(self, gcs_backend: GCSBackend) -> None:
        """Test deleting content decrements ref count."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock metadata with ref_count > 1
        mock_meta_blob = Mock()
        mock_meta_blob.exists.return_value = True
        mock_meta_blob.download_as_text.return_value = '{"ref_count": 2, "size": 100}'

        def blob_side_effect(path: str) -> Mock:
            if path.endswith(".meta"):
                return mock_meta_blob
            return mock_blob

        gcs_backend.bucket.blob.side_effect = blob_side_effect

        gcs_backend.delete_content("some_hash")

        # Should not delete blob, only update metadata
        mock_blob.delete.assert_not_called()
        assert mock_meta_blob.upload_from_string.call_count == 1

    def test_delete_content_last_reference(self, gcs_backend: GCSBackend) -> None:
        """Test deleting content with last reference."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock metadata with ref_count = 1
        mock_meta_blob = Mock()
        mock_meta_blob.exists.return_value = True
        mock_meta_blob.download_as_text.return_value = '{"ref_count": 1, "size": 100}'

        def blob_side_effect(path: str) -> Mock:
            if path.endswith(".meta"):
                return mock_meta_blob
            return mock_blob

        gcs_backend.bucket.blob.side_effect = blob_side_effect

        gcs_backend.delete_content("some_hash")

        # Should delete both content and metadata
        assert mock_blob.delete.call_count >= 1
        assert mock_meta_blob.delete.call_count >= 1

    def test_delete_content_not_found(self, gcs_backend: GCSBackend) -> None:
        """Test deleting non-existent content."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        with pytest.raises(NexusFileNotFoundError):
            gcs_backend.delete_content("nonexistent_hash")


class TestDirectoryOperations:
    """Test directory operations."""

    def test_mkdir_simple(self, gcs_backend: GCSBackend) -> None:
        """Test creating a simple directory."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        gcs_backend.mkdir("test_dir")

        # Should create directory marker
        mock_blob.upload_from_string.assert_called_once_with(
            "", content_type="application/x-directory", timeout=60
        )

    def test_mkdir_with_parents(self, gcs_backend: GCSBackend) -> None:
        """Test creating directory with parents."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        gcs_backend.mkdir("parent/child", parents=True)

        # Should create directory marker without checking parent
        mock_blob.upload_from_string.assert_called_once()

    def test_mkdir_already_exists(self, gcs_backend: GCSBackend) -> None:
        """Test creating directory that already exists."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Should raise error without exist_ok
        with pytest.raises(FileExistsError):
            gcs_backend.mkdir("existing_dir", exist_ok=False)

        # Should succeed with exist_ok
        gcs_backend.mkdir("existing_dir", exist_ok=True)
        mock_blob.upload_from_string.assert_not_called()

    def test_mkdir_parent_not_found(self, gcs_backend: GCSBackend) -> None:
        """Test creating directory when parent doesn't exist."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock is_directory to return False for parent
        gcs_backend.is_directory = Mock(return_value=False)

        with pytest.raises(FileNotFoundError):
            gcs_backend.mkdir("parent/child", parents=False)

    def test_mkdir_root(self, gcs_backend: GCSBackend) -> None:
        """Test creating root directory (no-op)."""
        gcs_backend.mkdir("")
        gcs_backend.mkdir("/")

        # Should not attempt to create anything
        gcs_backend.bucket.blob.assert_not_called()

    def test_rmdir_simple(self, gcs_backend: GCSBackend) -> None:
        """Test removing empty directory."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock list_blobs to return only the marker (empty dir)
        gcs_backend.client.list_blobs = Mock(return_value=[mock_blob])

        gcs_backend.rmdir("test_dir")

        # Should delete the marker
        mock_blob.delete.assert_called_once_with(timeout=60)

    def test_rmdir_not_empty(self, gcs_backend: GCSBackend) -> None:
        """Test removing non-empty directory without recursive."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock list_blobs to return multiple items (non-empty dir)
        mock_child = Mock()
        gcs_backend.client.list_blobs = Mock(return_value=[mock_blob, mock_child])

        with pytest.raises(OSError) as exc_info:
            gcs_backend.rmdir("test_dir", recursive=False)

        assert "not empty" in str(exc_info.value).lower()

    def test_rmdir_recursive(self, gcs_backend: GCSBackend) -> None:
        """Test removing directory recursively."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        # Mock list_blobs to return multiple items
        mock_child1 = Mock()
        mock_child2 = Mock()
        gcs_backend.client.list_blobs = Mock(return_value=[mock_child1, mock_child2])

        gcs_backend.rmdir("test_dir", recursive=True)

        # Should delete marker and all children
        mock_blob.delete.assert_called_once_with(timeout=60)
        mock_child1.delete.assert_called_once_with(timeout=60)
        mock_child2.delete.assert_called_once_with(timeout=60)

    def test_rmdir_not_found(self, gcs_backend: GCSBackend) -> None:
        """Test removing non-existent directory."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        with pytest.raises(NexusFileNotFoundError):
            gcs_backend.rmdir("nonexistent_dir")

    def test_rmdir_root_not_allowed(self, gcs_backend: GCSBackend) -> None:
        """Test that removing root directory is not allowed."""
        with pytest.raises(BackendError):
            gcs_backend.rmdir("")

        with pytest.raises(BackendError):
            gcs_backend.rmdir("/")

    def test_is_directory_true(self, gcs_backend: GCSBackend) -> None:
        """Test checking if directory exists."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        gcs_backend.bucket.blob.return_value = mock_blob

        result = gcs_backend.is_directory("test_dir")

        assert result is True

    def test_is_directory_false(self, gcs_backend: GCSBackend) -> None:
        """Test checking if directory doesn't exist."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        result = gcs_backend.is_directory("test_dir")

        assert result is False

    def test_is_directory_root(self, gcs_backend: GCSBackend) -> None:
        """Test that root is always a directory."""
        assert gcs_backend.is_directory("") is True
        assert gcs_backend.is_directory("/") is True


class TestHashOperations:
    """Test hash-related helper methods."""

    def test_compute_hash(self, gcs_backend: GCSBackend) -> None:
        """Test computing content hash (BLAKE3)."""
        content = b"Hello, World!"
        hash_result = gcs_backend._compute_hash(content)

        # Verify hash matches what hash_content produces
        expected = hash_content(content)
        assert hash_result == expected
        assert len(hash_result) == 64

    def test_hash_to_path(self, gcs_backend: GCSBackend) -> None:
        """Test converting hash to GCS path."""
        content_hash = "abcd1234567890ef"
        path = gcs_backend._hash_to_path(content_hash)

        # Should use two-level directory structure
        assert path == "cas/ab/cd/abcd1234567890ef"

    def test_hash_to_path_invalid_short(self, gcs_backend: GCSBackend) -> None:
        """Test hash_to_path with invalid short hash."""
        with pytest.raises(ValueError):
            gcs_backend._hash_to_path("abc")

    def test_get_meta_path(self, gcs_backend: GCSBackend) -> None:
        """Test getting metadata path."""
        content_hash = "abcd1234567890ef"
        meta_path = gcs_backend._get_meta_path(content_hash)

        assert meta_path == "cas/ab/cd/abcd1234567890ef.meta"


class TestMetadataOperations:
    """Test metadata read/write operations."""

    def test_read_metadata_exists(self, gcs_backend: GCSBackend) -> None:
        """Test reading existing metadata."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = '{"ref_count": 5, "size": 1024}'
        gcs_backend.bucket.blob.return_value = mock_blob

        metadata = gcs_backend._read_metadata("some_hash")

        assert metadata == {"ref_count": 5, "size": 1024}

    def test_read_metadata_not_exists(self, gcs_backend: GCSBackend) -> None:
        """Test reading non-existent metadata returns defaults."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_backend.bucket.blob.return_value = mock_blob

        metadata = gcs_backend._read_metadata("some_hash")

        assert metadata == {"ref_count": 0, "size": 0}

    def test_write_metadata(self, gcs_backend: GCSBackend) -> None:
        """Test writing metadata."""
        mock_blob = Mock()
        gcs_backend.bucket.blob.return_value = mock_blob

        metadata = {"ref_count": 3, "size": 512}
        gcs_backend._write_metadata("some_hash", metadata)

        # Should upload JSON
        mock_blob.upload_from_string.assert_called_once()
        call_args = mock_blob.upload_from_string.call_args
        assert '"ref_count": 3' in call_args[0][0]
        assert '"size": 512' in call_args[0][0]
