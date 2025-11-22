"""Unit tests for S3 connector backend with versioning support."""

from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from nexus.backends.s3_connector import S3ConnectorBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.permissions import OperationContext


@pytest.fixture
def mock_boto3():
    """Create mock boto3 client and resource."""
    with patch("nexus.backends.s3_connector.boto3") as mock_boto3:
        mock_client = Mock()
        mock_resource = Mock()
        mock_bucket = Mock()

        # Default: bucket exists, no versioning
        mock_client.head_bucket.return_value = {}
        mock_client.get_bucket_versioning.return_value = {}
        mock_resource.Bucket.return_value = mock_bucket

        mock_boto3.client.return_value = mock_client
        mock_boto3.resource.return_value = mock_resource

        yield mock_boto3


@pytest.fixture
def s3_connector_backend(mock_boto3: Mock) -> S3ConnectorBackend:
    """Create an S3 connector backend instance with mocked client (no versioning)."""
    return S3ConnectorBackend(
        bucket_name="test-bucket", region_name="us-east-1", prefix="test-prefix"
    )


@pytest.fixture
def s3_connector_versioned(mock_boto3: Mock) -> S3ConnectorBackend:
    """Create an S3 connector backend with versioning enabled."""
    mock_client = mock_boto3.client.return_value
    mock_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    return S3ConnectorBackend(bucket_name="test-bucket-versioned", region_name="us-east-1")


class TestS3ConnectorInitialization:
    """Test S3 connector backend initialization."""

    def test_init_with_versioning_disabled(self, mock_boto3: Mock) -> None:
        """Test initialization with versioning disabled."""
        mock_client = mock_boto3.client.return_value
        mock_client.get_bucket_versioning.return_value = {}

        backend = S3ConnectorBackend(bucket_name="test-bucket")

        assert backend.bucket_name == "test-bucket"
        assert backend.versioning_enabled is False
        assert backend.prefix == ""

    def test_init_with_versioning_enabled(self, mock_boto3: Mock) -> None:
        """Test initialization with versioning enabled."""
        mock_client = mock_boto3.client.return_value
        mock_client.get_bucket_versioning.return_value = {"Status": "Enabled"}

        backend = S3ConnectorBackend(bucket_name="test-bucket-versioned")

        assert backend.bucket_name == "test-bucket-versioned"
        assert backend.versioning_enabled is True

    def test_init_with_prefix(self, mock_boto3: Mock) -> None:
        """Test initialization with prefix."""
        backend = S3ConnectorBackend(bucket_name="test-bucket", prefix="my-prefix/")

        assert backend.prefix == "my-prefix"  # Trailing slash removed

    def test_init_bucket_not_exists(self, mock_boto3: Mock) -> None:
        """Test initialization fails when bucket doesn't exist."""
        mock_client = mock_boto3.client.return_value
        error_response = {"Error": {"Code": "404"}}
        mock_client.head_bucket.side_effect = ClientError(error_response, "HeadBucket")

        with pytest.raises(BackendError) as exc_info:
            S3ConnectorBackend(bucket_name="nonexistent-bucket")

        assert "does not exist" in str(exc_info.value)

    def test_init_with_explicit_credentials(self, mock_boto3: Mock) -> None:
        """Test initialization with explicit credentials."""
        backend = S3ConnectorBackend(
            bucket_name="test-bucket",
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        )

        assert backend.bucket_name == "test-bucket"
        # Verify boto3.client was called with credentials
        mock_boto3.client.assert_called()


class TestContentTypeDetection:
    """Test Content-Type detection for S3 uploads."""

    def test_detect_text_plain_with_utf8(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test UTF-8 text file gets charset=utf-8."""
        content = b"Hello, World!"
        content_type = s3_connector_backend._detect_content_type("file.txt", content)
        assert content_type == "text/plain; charset=utf-8"

    def test_detect_python_file_with_utf8(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test Python file gets text/x-python with charset=utf-8."""
        content = b"#!/usr/bin/env python3\nprint('Hello')"
        content_type = s3_connector_backend._detect_content_type("script.py", content)
        assert content_type == "text/x-python; charset=utf-8"

    def test_detect_json_file(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test JSON file gets application/json (no charset needed per spec)."""
        content = b'{"key": "value"}'
        content_type = s3_connector_backend._detect_content_type("data.json", content)
        # JSON is detected as application/json (charset not needed - JSON is always UTF-8)
        assert content_type == "application/json"

    def test_detect_markdown_file_with_utf8(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test Markdown file gets text type with charset=utf-8."""
        content = b"# Markdown Header\n\nSome text."
        content_type = s3_connector_backend._detect_content_type("README.md", content)
        assert "charset=utf-8" in content_type

    def test_detect_binary_file_no_charset(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test binary file gets appropriate type without charset."""
        # PNG magic bytes
        content = b"\x89PNG\r\n\x1a\n"
        content_type = s3_connector_backend._detect_content_type("image.png", content)
        assert content_type == "image/png"
        assert "charset" not in content_type

    def test_detect_non_utf8_binary_fallback(
        self, s3_connector_backend: S3ConnectorBackend
    ) -> None:
        """Test non-UTF-8 binary content falls back to octet-stream."""
        # Invalid UTF-8 sequence
        content = b"\xff\xfe\x00\x01\x02"
        content_type = s3_connector_backend._detect_content_type("unknown.dat", content)
        assert content_type == "application/octet-stream"
        assert "charset" not in content_type


class TestWriteContentWithoutVersioning:
    """Test write_content without S3 versioning."""

    def test_write_content_returns_hash(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test write_content returns SHA-256 hash when versioning disabled."""
        test_content = b"Hello, S3 Connector!"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # Mock the put_object response
        s3_connector_backend.client.put_object.return_value = {}

        result = s3_connector_backend.write_content(test_content, context=context)

        # Should return SHA-256 hash (64 chars)
        assert len(result) == 64
        int(result, 16)  # Verify it's hex

        # Should upload to correct path with proper Content-Type
        s3_connector_backend.client.put_object.assert_called_once()
        call_kwargs = s3_connector_backend.client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == "test-prefix/file.txt"
        assert call_kwargs["Body"] == test_content
        assert call_kwargs["ContentType"] == "text/plain; charset=utf-8"

    def test_write_content_without_context(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test write_content fails without context."""
        with pytest.raises(ValueError) as exc_info:
            s3_connector_backend.write_content(b"test")

        assert "backend_path" in str(exc_info.value)

    def test_write_content_without_backend_path(
        self, s3_connector_backend: S3ConnectorBackend
    ) -> None:
        """Test write_content fails without backend_path."""
        context = OperationContext(user="test_user", groups=[])

        with pytest.raises(ValueError) as exc_info:
            s3_connector_backend.write_content(b"test", context=context)

        assert "backend_path" in str(exc_info.value)


class TestWriteContentWithVersioning:
    """Test write_content with S3 versioning enabled."""

    def test_write_content_returns_version_id(
        self, s3_connector_versioned: S3ConnectorBackend
    ) -> None:
        """Test write_content returns version ID when versioning enabled."""
        test_content = b"Hello, versioned S3!"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # Mock response with version ID
        s3_connector_versioned.client.put_object.return_value = {"VersionId": "abc123version"}

        result = s3_connector_versioned.write_content(test_content, context=context)

        # Should return version ID
        assert result == "abc123version"

    def test_write_content_multiple_versions(
        self, s3_connector_versioned: S3ConnectorBackend
    ) -> None:
        """Test writing multiple versions returns different version IDs."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # First write
        s3_connector_versioned.client.put_object.return_value = {"VersionId": "version1"}
        ver1 = s3_connector_versioned.write_content(b"version 1", context=context)

        # Second write (same path)
        s3_connector_versioned.client.put_object.return_value = {"VersionId": "version2"}
        ver2 = s3_connector_versioned.write_content(b"version 2", context=context)

        assert ver1 == "version1"
        assert ver2 == "version2"
        assert ver1 != ver2


class TestReadContentWithoutVersioning:
    """Test read_content without S3 versioning."""

    def test_read_content_ignores_hash(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test read_content ignores hash and reads from backend_path."""
        test_content = b"Current content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_body = Mock()
        mock_body.read.return_value = test_content
        s3_connector_backend.client.get_object.return_value = {"Body": mock_body}

        # Pass any hash - should be ignored
        result = s3_connector_backend.read_content("any_hash_value", context=context)

        assert result == test_content
        # Should read from backend_path, not hash
        s3_connector_backend.client.get_object.assert_called_once_with(
            Bucket="test-bucket", Key="test-prefix/file.txt"
        )

    def test_read_content_not_found(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test read_content raises error when file not found."""
        context = OperationContext(user="test_user", groups=[], backend_path="missing.txt")

        error_response = {"Error": {"Code": "NoSuchKey"}}
        s3_connector_backend.client.get_object.side_effect = ClientError(
            error_response, "GetObject"
        )

        with pytest.raises(NexusFileNotFoundError):
            s3_connector_backend.read_content("any_hash", context=context)


class TestReadContentWithVersioning:
    """Test read_content with S3 versioning enabled."""

    def test_read_specific_version(self, s3_connector_versioned: S3ConnectorBackend) -> None:
        """Test read_content retrieves specific version."""
        old_content = b"Version 1 content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_body = Mock()
        mock_body.read.return_value = old_content
        s3_connector_versioned.client.get_object.return_value = {"Body": mock_body}

        # Read old version by version ID
        result = s3_connector_versioned.read_content("version123", context=context)

        assert result == old_content
        # Should request specific version
        s3_connector_versioned.client.get_object.assert_called_with(
            Bucket="test-bucket-versioned", Key="file.txt", VersionId="version123"
        )

    def test_read_current_version_with_hash(
        self, s3_connector_versioned: S3ConnectorBackend
    ) -> None:
        """Test read_content reads current when identifier is hex hash (not version ID)."""
        current_content = b"Current version"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_body = Mock()
        mock_body.read.return_value = current_content
        s3_connector_versioned.client.get_object.return_value = {"Body": mock_body}

        # Hash-like identifier (hex string, 64 chars)
        hex_hash = "a" * 64
        result = s3_connector_versioned.read_content(hex_hash, context=context)

        assert result == current_content
        # Should read current version (no VersionId parameter)
        s3_connector_versioned.client.get_object.assert_called_with(
            Bucket="test-bucket-versioned", Key="file.txt"
        )


class TestPathMapping:
    """Test path mapping with prefix."""

    def test_get_s3_path_with_prefix(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test path mapping with prefix."""
        result = s3_connector_backend._get_blob_path("dir/file.txt")
        assert result == "test-prefix/dir/file.txt"

    def test_get_s3_path_without_prefix(self, mock_boto3: Mock) -> None:
        """Test path mapping without prefix."""
        backend = S3ConnectorBackend(bucket_name="test-bucket", prefix="")
        result = backend._get_blob_path("dir/file.txt")
        assert result == "dir/file.txt"

    def test_get_s3_path_leading_slash(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test path mapping strips leading slash."""
        result = s3_connector_backend._get_blob_path("/dir/file.txt")
        assert result == "test-prefix/dir/file.txt"


class TestDeleteContent:
    """Test delete_content operations."""

    def test_delete_content_success(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test successful content deletion."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # Mock head_object to indicate file exists
        s3_connector_backend.client.head_object.return_value = {}
        s3_connector_backend.client.delete_object.return_value = {}

        s3_connector_backend.delete_content("any_hash", context=context)

        # Should check existence then delete
        s3_connector_backend.client.head_object.assert_called_once()
        s3_connector_backend.client.delete_object.assert_called_once()

    def test_delete_content_not_found(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test delete_content raises error when file not found."""
        context = OperationContext(user="test_user", groups=[], backend_path="missing.txt")

        error_response = {"Error": {"Code": "404"}}
        s3_connector_backend.client.head_object.side_effect = ClientError(
            error_response, "HeadObject"
        )

        with pytest.raises(NexusFileNotFoundError):
            s3_connector_backend.delete_content("any_hash", context=context)


class TestDirectoryOperations:
    """Test S3 directory operations."""

    def test_mkdir_creates_marker(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test mkdir creates directory marker."""
        # Mock head_object to indicate directory doesn't exist
        error_response = {"Error": {"Code": "404"}}
        s3_connector_backend.client.head_object.side_effect = ClientError(
            error_response, "HeadObject"
        )
        s3_connector_backend.client.put_object.return_value = {}

        s3_connector_backend.mkdir("newdir", parents=True, exist_ok=True)

        # Should create directory marker with trailing slash
        s3_connector_backend.client.put_object.assert_called_once()
        call_kwargs = s3_connector_backend.client.put_object.call_args[1]
        assert call_kwargs["Key"].endswith("/")
        assert call_kwargs["ContentType"] == "application/x-directory"

    def test_mkdir_exist_ok_false(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test mkdir raises error when directory exists and exist_ok=False."""
        # Mock head_object to indicate directory exists
        s3_connector_backend.client.head_object.return_value = {}

        with pytest.raises(FileExistsError):
            s3_connector_backend.mkdir("existingdir", exist_ok=False)

    def test_is_directory_with_marker(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test is_directory returns True when marker exists."""
        # Mock head_object to indicate marker exists
        s3_connector_backend.client.head_object.return_value = {}

        result = s3_connector_backend.is_directory("mydir")

        assert result is True

    def test_is_directory_virtual(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test is_directory returns True for virtual directory."""
        # Mock head_object to fail (no marker)
        error_response = {"Error": {"Code": "404"}}
        s3_connector_backend.client.head_object.side_effect = ClientError(
            error_response, "HeadObject"
        )
        # But list_objects returns children
        s3_connector_backend.client.list_objects_v2.return_value = {
            "Contents": [{"Key": "test-prefix/mydir/file.txt"}]
        }

        result = s3_connector_backend.is_directory("mydir")

        assert result is True

    def test_list_dir(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test list_dir returns directory contents."""
        # Mock is_directory check
        s3_connector_backend.client.head_object.return_value = {}

        # Mock list response
        s3_connector_backend.client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "test-prefix/mydir/file1.txt"},
                {"Key": "test-prefix/mydir/file2.txt"},
            ],
            "CommonPrefixes": [{"Prefix": "test-prefix/mydir/subdir/"}],
        }

        result = s3_connector_backend.list_dir("mydir")

        assert "file1.txt" in result
        assert "file2.txt" in result
        assert "subdir/" in result


class TestRenameOperations:
    """Test S3 file rename/move operations."""

    def test_rename_file_success(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test successful file rename."""

        # Source exists, destination doesn't
        def head_side_effect(Bucket, Key):
            if Key == "test-prefix/old.txt":
                return {}
            else:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

        s3_connector_backend.client.head_object.side_effect = head_side_effect
        s3_connector_backend.client.copy_object.return_value = {}
        s3_connector_backend.client.delete_object.return_value = {}

        s3_connector_backend.rename_file("old.txt", "new.txt")

        # Should copy then delete
        s3_connector_backend.client.copy_object.assert_called_once()
        s3_connector_backend.client.delete_object.assert_called_once()

    def test_rename_file_source_not_found(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test rename raises error when source doesn't exist."""
        error_response = {"Error": {"Code": "404"}}
        s3_connector_backend.client.head_object.side_effect = ClientError(
            error_response, "HeadObject"
        )

        with pytest.raises(FileNotFoundError):
            s3_connector_backend.rename_file("missing.txt", "new.txt")

    def test_rename_file_dest_exists(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test rename raises error when destination exists."""
        # Both files exist
        s3_connector_backend.client.head_object.return_value = {}

        with pytest.raises(FileExistsError):
            s3_connector_backend.rename_file("old.txt", "existing.txt")


class TestContentExists:
    """Test content_exists operations."""

    def test_content_exists_true(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test content_exists returns True when file exists."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")
        s3_connector_backend.client.head_object.return_value = {}

        result = s3_connector_backend.content_exists("any_hash", context=context)

        assert result is True

    def test_content_exists_false(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test content_exists returns False when file doesn't exist."""
        context = OperationContext(user="test_user", groups=[], backend_path="missing.txt")
        error_response = {"Error": {"Code": "404"}}
        s3_connector_backend.client.head_object.side_effect = ClientError(
            error_response, "HeadObject"
        )

        result = s3_connector_backend.content_exists("any_hash", context=context)

        assert result is False


class TestGetContentSize:
    """Test get_content_size operations."""

    def test_get_content_size_success(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test successful content size retrieval."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")
        s3_connector_backend.client.head_object.return_value = {"ContentLength": 1024}

        result = s3_connector_backend.get_content_size("any_hash", context=context)

        assert result == 1024

    def test_get_content_size_not_found(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test get_content_size raises error when file not found."""
        context = OperationContext(user="test_user", groups=[], backend_path="missing.txt")
        error_response = {"Error": {"Code": "404"}}
        s3_connector_backend.client.head_object.side_effect = ClientError(
            error_response, "HeadObject"
        )

        with pytest.raises(NexusFileNotFoundError):
            s3_connector_backend.get_content_size("any_hash", context=context)


class TestRefCount:
    """Test reference count operations."""

    def test_get_ref_count_always_one(self, s3_connector_backend: S3ConnectorBackend) -> None:
        """Test ref count always returns 1 (no deduplication)."""
        result = s3_connector_backend.get_ref_count("any_hash")

        assert result == 1
