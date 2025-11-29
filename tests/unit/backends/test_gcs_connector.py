"""Unit tests for GCS connector backend with versioning support."""

from unittest.mock import Mock, patch

import pytest

from nexus.backends.gcs_connector import GCSConnectorBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.permissions import OperationContext


@pytest.fixture
def mock_storage_client() -> Mock:
    """Create a mock GCS storage client."""
    with patch("nexus.backends.gcs_connector.storage") as mock_storage:
        mock_client = Mock()
        mock_bucket = Mock()
        mock_bucket.exists.return_value = True
        mock_bucket.versioning_enabled = False  # Default: no versioning
        mock_client.bucket.return_value = mock_bucket
        mock_storage.Client.return_value = mock_client
        yield mock_storage


@pytest.fixture
def gcs_connector_backend(mock_storage_client: Mock) -> GCSConnectorBackend:
    """Create a GCS connector backend instance with mocked client (no versioning)."""
    return GCSConnectorBackend(
        bucket_name="test-bucket", project_id="test-project", prefix="test-prefix"
    )


@pytest.fixture
def gcs_connector_versioned(mock_storage_client: Mock) -> GCSConnectorBackend:
    """Create a GCS connector backend with versioning enabled."""
    mock_bucket = mock_storage_client.Client.return_value.bucket.return_value
    mock_bucket.versioning_enabled = True
    return GCSConnectorBackend(bucket_name="test-bucket-versioned", project_id="test-project")


class TestGCSConnectorInitialization:
    """Test GCS connector backend initialization."""

    def test_init_with_versioning_disabled(self, mock_storage_client: Mock) -> None:
        """Test initialization with versioning disabled."""
        mock_bucket = mock_storage_client.Client.return_value.bucket.return_value
        mock_bucket.versioning_enabled = False

        backend = GCSConnectorBackend(bucket_name="test-bucket")

        assert backend.bucket_name == "test-bucket"
        assert backend.versioning_enabled is False
        assert backend.prefix == ""

    def test_init_with_versioning_enabled(self, mock_storage_client: Mock) -> None:
        """Test initialization with versioning enabled."""
        mock_bucket = mock_storage_client.Client.return_value.bucket.return_value
        mock_bucket.versioning_enabled = True

        backend = GCSConnectorBackend(bucket_name="test-bucket-versioned")

        assert backend.bucket_name == "test-bucket-versioned"
        assert backend.versioning_enabled is True

    def test_init_with_prefix(self, mock_storage_client: Mock) -> None:
        """Test initialization with prefix."""
        backend = GCSConnectorBackend(bucket_name="test-bucket", prefix="my-prefix/")

        assert backend.prefix == "my-prefix"  # Trailing slash removed

    def test_init_bucket_not_exists(self, mock_storage_client: Mock) -> None:
        """Test initialization fails when bucket doesn't exist."""
        mock_client = Mock()
        mock_bucket = Mock()
        mock_bucket.exists.return_value = False
        mock_client.bucket.return_value = mock_bucket
        mock_storage_client.Client.return_value = mock_client

        with pytest.raises(BackendError) as exc_info:
            GCSConnectorBackend(bucket_name="nonexistent-bucket")

        assert "does not exist" in str(exc_info.value)


class TestContentTypeDetection:
    """Test Content-Type detection for GCS uploads."""

    def test_detect_text_plain_with_utf8(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test UTF-8 text file gets charset=utf-8."""
        content = b"Hello, World!"
        content_type = gcs_connector_backend._detect_content_type("file.txt", content)
        assert content_type == "text/plain; charset=utf-8"

    def test_detect_python_file_with_utf8(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test Python file gets text/x-python with charset=utf-8."""
        content = b"#!/usr/bin/env python3\nprint('Hello')"
        content_type = gcs_connector_backend._detect_content_type("script.py", content)
        assert content_type == "text/x-python; charset=utf-8"

    def test_detect_json_file(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test JSON file gets application/json (no charset needed per spec)."""
        content = b'{"key": "value"}'
        content_type = gcs_connector_backend._detect_content_type("data.json", content)
        # JSON is detected as application/json (charset not needed - JSON is always UTF-8)
        assert content_type == "application/json"

    def test_detect_markdown_file_with_utf8(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test Markdown file gets text type with charset=utf-8."""
        content = b"# Markdown Header\n\nSome text."
        content_type = gcs_connector_backend._detect_content_type("README.md", content)
        assert "charset=utf-8" in content_type

    def test_detect_binary_file_no_charset(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test binary file gets appropriate type without charset."""
        # PNG magic bytes
        content = b"\x89PNG\r\n\x1a\n"
        content_type = gcs_connector_backend._detect_content_type("image.png", content)
        assert content_type == "image/png"
        assert "charset" not in content_type

    def test_detect_non_utf8_binary_fallback(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test non-UTF-8 binary content falls back to octet-stream."""
        # Invalid UTF-8 sequence
        content = b"\xff\xfe\x00\x01\x02"
        content_type = gcs_connector_backend._detect_content_type("unknown.dat", content)
        assert content_type == "application/octet-stream"
        assert "charset" not in content_type

    def test_detect_pdf_file(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test PDF file gets correct MIME type."""
        # PDF magic bytes
        content = b"%PDF-1.4"
        content_type = gcs_connector_backend._detect_content_type("document.pdf", content)
        assert content_type == "application/pdf"

    def test_detect_unknown_extension_utf8(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test unknown extension with UTF-8 content defaults to text/plain."""
        content = b"This is plain text"
        content_type = gcs_connector_backend._detect_content_type("file.unknown", content)
        assert content_type == "text/plain; charset=utf-8"


class TestWriteContentWithoutVersioning:
    """Test write_content without GCS versioning."""

    def test_write_content_returns_hash(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test write_content returns SHA-256 hash when versioning disabled."""
        test_content = b"Hello, GCS Connector!"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        result = gcs_connector_backend.write_content(test_content, context=context)

        # Should return SHA-256 hash (64 chars)
        assert len(result) == 64
        int(result, 16)  # Verify it's hex

        # Should upload to correct path with proper Content-Type
        gcs_connector_backend.bucket.blob.assert_called_with("test-prefix/file.txt")
        # Check that upload_from_string was called with correct content and content_type
        assert mock_blob.upload_from_string.call_count == 1
        call_args = mock_blob.upload_from_string.call_args
        assert call_args[0][0] == test_content  # First positional arg
        assert call_args[1]["content_type"] == "text/plain; charset=utf-8"
        assert call_args[1]["timeout"] == 60

    def test_write_content_without_context(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test write_content fails without context."""
        with pytest.raises(ValueError) as exc_info:
            gcs_connector_backend.write_content(b"test")

        assert "backend_path" in str(exc_info.value)

    def test_write_content_without_backend_path(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test write_content fails without backend_path."""
        context = OperationContext(user="test_user", groups=[])

        with pytest.raises(ValueError) as exc_info:
            gcs_connector_backend.write_content(b"test", context=context)

        assert "backend_path" in str(exc_info.value)


class TestWriteContentWithVersioning:
    """Test write_content with GCS versioning enabled."""

    def test_write_content_returns_generation(
        self, gcs_connector_versioned: GCSConnectorBackend
    ) -> None:
        """Test write_content returns generation number when versioning enabled."""
        test_content = b"Hello, versioned GCS!"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        mock_blob.generation = 1234567890  # GCS generation number
        gcs_connector_versioned.bucket.blob.return_value = mock_blob

        result = gcs_connector_versioned.write_content(test_content, context=context)

        # Should return generation number as string
        assert result == "1234567890"

        # Should upload with proper Content-Type and reload to get generation
        # Check that upload_from_string was called with correct content and content_type
        assert mock_blob.upload_from_string.call_count == 1
        call_args = mock_blob.upload_from_string.call_args
        assert call_args[0][0] == test_content  # First positional arg
        assert call_args[1]["content_type"] == "text/plain; charset=utf-8"
        assert call_args[1]["timeout"] == 60
        mock_blob.reload.assert_called_once()

    def test_write_content_multiple_versions(
        self, gcs_connector_versioned: GCSConnectorBackend
    ) -> None:
        """Test writing multiple versions returns different generations."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # First write
        mock_blob1 = Mock()
        mock_blob1.generation = 1000
        gcs_connector_versioned.bucket.blob.return_value = mock_blob1
        gen1 = gcs_connector_versioned.write_content(b"version 1", context=context)

        # Second write (same path)
        mock_blob2 = Mock()
        mock_blob2.generation = 2000
        gcs_connector_versioned.bucket.blob.return_value = mock_blob2
        gen2 = gcs_connector_versioned.write_content(b"version 2", context=context)

        assert gen1 == "1000"
        assert gen2 == "2000"
        assert gen1 != gen2


class TestReadContentWithoutVersioning:
    """Test read_content without GCS versioning."""

    def test_read_content_ignores_hash(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test read_content ignores hash and reads from backend_path."""
        test_content = b"Current content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = test_content
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        # Pass any hash - should be ignored
        result = gcs_connector_backend.read_content("any_hash_value", context=context)

        assert result == test_content
        # Should read from backend_path, not hash
        gcs_connector_backend.bucket.blob.assert_called_with("test-prefix/file.txt")

    def test_read_content_returns_current_for_old_versions(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test read_content always returns current content (no versioning)."""
        current_content = b"Latest version"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = current_content
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        # Try reading with different "old version" hashes
        result1 = gcs_connector_backend.read_content("old_hash_v1", context=context)
        result2 = gcs_connector_backend.read_content("old_hash_v2", context=context)
        result3 = gcs_connector_backend.read_content("current_hash", context=context)

        # All should return current content
        assert result1 == current_content
        assert result2 == current_content
        assert result3 == current_content

    def test_read_content_not_found(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test read_content raises error when file not found."""
        context = OperationContext(user="test_user", groups=[], backend_path="missing.txt")

        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        with pytest.raises(NexusFileNotFoundError):
            gcs_connector_backend.read_content("any_hash", context=context)


class TestReadContentWithVersioning:
    """Test read_content with GCS versioning enabled."""

    def test_read_specific_generation(self, gcs_connector_versioned: GCSConnectorBackend) -> None:
        """Test read_content retrieves specific generation."""
        old_content = b"Version 1 content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = old_content
        gcs_connector_versioned.bucket.blob.return_value = mock_blob

        # Read old version by generation number
        result = gcs_connector_versioned.read_content("1000", context=context)

        assert result == old_content
        # Should create blob with specific generation
        gcs_connector_versioned.bucket.blob.assert_called_with("file.txt", generation=1000)

    def test_read_current_version_with_hash(
        self, gcs_connector_versioned: GCSConnectorBackend
    ) -> None:
        """Test read_content reads current when identifier is hash (not generation)."""
        current_content = b"Current version"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = current_content
        gcs_connector_versioned.bucket.blob.return_value = mock_blob

        # Hash-like identifier (hex string, not numeric)
        result = gcs_connector_versioned.read_content("abc123def456", context=context)

        assert result == current_content
        # Should read current version (no generation parameter)
        gcs_connector_versioned.bucket.blob.assert_called_with("file.txt")

    def test_read_version_not_found(self, gcs_connector_versioned: GCSConnectorBackend) -> None:
        """Test read_content raises error when version not found."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_connector_versioned.bucket.blob.return_value = mock_blob

        with pytest.raises(NexusFileNotFoundError):
            gcs_connector_versioned.read_content("9999", context=context)


class TestVersioningIntegration:
    """Test versioning behavior end-to-end."""

    def test_no_versioning_overwrites_content(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test that without versioning, writes overwrite content."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        # Write v1
        hash1 = gcs_connector_backend.write_content(b"version 1", context=context)
        # Write v2 (overwrites)
        hash2 = gcs_connector_backend.write_content(b"version 2", context=context)

        # Both writes go to same path
        assert gcs_connector_backend.bucket.blob.call_count == 2
        for call in gcs_connector_backend.bucket.blob.call_args_list:
            assert call[0][0] == "test-prefix/file.txt"

        # Returns different hashes (content changed)
        assert hash1 != hash2

    def test_versioning_preserves_old_content(
        self, gcs_connector_versioned: GCSConnectorBackend
    ) -> None:
        """Test that with versioning, old content is preserved."""
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # Mock writes with different generations
        mock_blob_v1 = Mock()
        mock_blob_v1.generation = 1000
        mock_blob_v2 = Mock()
        mock_blob_v2.generation = 2000

        write_calls = [mock_blob_v1, mock_blob_v2]
        gcs_connector_versioned.bucket.blob.side_effect = write_calls

        # Write v1
        gen1 = gcs_connector_versioned.write_content(b"version 1", context=context)
        # Write v2
        gen2 = gcs_connector_versioned.write_content(b"version 2", context=context)

        # Different generations returned
        assert gen1 == "1000"
        assert gen2 == "2000"

        # Now set up reads for both versions
        mock_blob_read_v1 = Mock()
        mock_blob_read_v1.exists.return_value = True
        mock_blob_read_v1.download_as_bytes.return_value = b"version 1"

        mock_blob_read_v2 = Mock()
        mock_blob_read_v2.exists.return_value = True
        mock_blob_read_v2.download_as_bytes.return_value = b"version 2"

        def blob_side_effect(path: str, generation: int | None = None) -> Mock:
            if generation == 1000:
                return mock_blob_read_v1
            elif generation == 2000:
                return mock_blob_read_v2
            else:
                return mock_blob_read_v2  # Default to latest

        gcs_connector_versioned.bucket.blob.side_effect = blob_side_effect

        # Read old version
        content_v1 = gcs_connector_versioned.read_content(gen1, context=context)
        # Read new version
        content_v2 = gcs_connector_versioned.read_content(gen2, context=context)

        # Both versions should be readable
        assert content_v1 == b"version 1"
        assert content_v2 == b"version 2"


class TestPathMapping:
    """Test path mapping with prefix."""

    def test_get_gcs_path_with_prefix(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test path mapping with prefix."""
        result = gcs_connector_backend._get_blob_path("dir/file.txt")
        assert result == "test-prefix/dir/file.txt"

    def test_get_gcs_path_without_prefix(self, mock_storage_client: Mock) -> None:
        """Test path mapping without prefix."""
        backend = GCSConnectorBackend(bucket_name="test-bucket", prefix="")
        result = backend._get_blob_path("dir/file.txt")
        assert result == "dir/file.txt"

    def test_get_gcs_path_leading_slash(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test path mapping strips leading slash."""
        result = gcs_connector_backend._get_blob_path("/dir/file.txt")
        assert result == "test-prefix/dir/file.txt"


class TestGetVersion:
    """Test get_version method for GCS generation numbers."""

    def test_get_version_returns_generation(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test get_version returns GCS generation number."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.generation = 1234567890
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")
        result = gcs_connector_backend.get_version("/file.txt", context=context)

        assert result == "1234567890"
        gcs_connector_backend.bucket.blob.assert_called_with("test-prefix/file.txt")

    def test_get_version_file_not_exists(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test get_version returns None for non-existent file."""
        mock_blob = Mock()
        mock_blob.exists.return_value = False
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        context = OperationContext(user="test_user", groups=[], backend_path="missing.txt")
        result = gcs_connector_backend.get_version("/missing.txt", context=context)

        assert result is None

    def test_get_version_uses_backend_path_from_context(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test get_version prefers backend_path from context."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.generation = 9999
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        context = OperationContext(user="test_user", groups=[], backend_path="context/path.txt")
        result = gcs_connector_backend.get_version("/different/path.txt", context=context)

        assert result == "9999"
        # Should use backend_path from context, not the path argument
        gcs_connector_backend.bucket.blob.assert_called_with("test-prefix/context/path.txt")

    def test_get_version_without_context(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test get_version uses path argument when no context."""
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.generation = 5555
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        result = gcs_connector_backend.get_version("dir/file.txt")

        assert result == "5555"
        gcs_connector_backend.bucket.blob.assert_called_with("test-prefix/dir/file.txt")


class TestCacheMixinInheritance:
    """Test that GCSConnectorBackend inherits from CacheConnectorMixin."""

    def test_has_cache_mixin_methods(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test GCSConnectorBackend has CacheConnectorMixin methods."""
        # Check mixin methods exist
        assert hasattr(gcs_connector_backend, "_read_from_cache")
        assert hasattr(gcs_connector_backend, "_write_to_cache")
        assert hasattr(gcs_connector_backend, "_invalidate_cache")
        assert hasattr(gcs_connector_backend, "_check_version")
        assert hasattr(gcs_connector_backend, "sync")

    def test_db_session_attribute(self, mock_storage_client: Mock) -> None:
        """Test db_session is stored as attribute."""
        mock_session = Mock()
        backend = GCSConnectorBackend(
            bucket_name="test-bucket",
            db_session=mock_session,
        )
        assert backend.db_session is mock_session

    def test_db_session_default_none(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test db_session defaults to None."""
        assert gcs_connector_backend.db_session is None


class TestReadContentWithCaching:
    """Test read_content with caching support."""

    def test_read_from_backend_when_no_db_session(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test reads from backend when db_session is None."""
        test_content = b"Backend content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_bytes.return_value = test_content
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        result = gcs_connector_backend.read_content("any_hash", context=context)

        assert result == test_content
        mock_blob.download_as_bytes.assert_called_once()

    def test_read_content_override_includes_caching_logic(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test that read_content is overridden with caching logic."""
        # The read_content method should be overridden in GCSConnectorBackend
        # to include caching, not inherited from BaseBlobStorageConnector
        defining_class = type(gcs_connector_backend).read_content

        # Should be defined in GCSConnectorBackend, not BaseBlobStorageConnector
        assert "GCSConnectorBackend" in str(defining_class)


class TestWriteContentWithCaching:
    """Test write_content with caching support."""

    def test_write_content_override_includes_cache_invalidation(
        self, gcs_connector_backend: GCSConnectorBackend
    ) -> None:
        """Test that write_content is overridden with cache invalidation."""
        # The write_content method should be overridden in GCSConnectorBackend
        defining_class = type(gcs_connector_backend).write_content

        # Should be defined in GCSConnectorBackend, not BaseBlobStorageConnector
        assert "GCSConnectorBackend" in str(defining_class)

    def test_write_content_returns_hash(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test write_content returns hash when no versioning."""
        test_content = b"New content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        result = gcs_connector_backend.write_content(test_content, context=context)

        # Should return hash (no versioning)
        assert len(result) == 64
        mock_blob.upload_from_string.assert_called_once()


class TestWriteContentWithVersionCheck:
    """Test write_content_with_version_check method."""

    def test_write_with_version_check_success(
        self, gcs_connector_versioned: GCSConnectorBackend
    ) -> None:
        """Test write succeeds when version matches."""
        test_content = b"Updated content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # Mock get_version to return expected version
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.generation = 1000
        gcs_connector_versioned.bucket.blob.return_value = mock_blob

        # Write with correct expected_version
        result = gcs_connector_versioned.write_content_with_version_check(
            test_content, context=context, expected_version="1000"
        )

        # Should succeed and return new generation
        assert result == "1000"

    def test_write_with_version_check_conflict(
        self, gcs_connector_versioned: GCSConnectorBackend
    ) -> None:
        """Test write fails when version doesn't match."""
        from nexus.core.exceptions import ConflictError

        test_content = b"Updated content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        # Mock get_version to return different version
        mock_blob = Mock()
        mock_blob.exists.return_value = True
        mock_blob.generation = 2000  # Different from expected
        gcs_connector_versioned.bucket.blob.return_value = mock_blob

        # Write with wrong expected_version should raise ConflictError
        with pytest.raises(ConflictError):
            gcs_connector_versioned.write_content_with_version_check(
                test_content, context=context, expected_version="1000"
            )

    def test_write_without_version_check(self, gcs_connector_backend: GCSConnectorBackend) -> None:
        """Test write_content_with_version_check without expected_version."""
        test_content = b"New content"
        context = OperationContext(user="test_user", groups=[], backend_path="file.txt")

        mock_blob = Mock()
        gcs_connector_backend.bucket.blob.return_value = mock_blob

        # No expected_version means skip version check
        result = gcs_connector_backend.write_content_with_version_check(
            test_content, context=context, expected_version=None
        )

        # Should return hash (no versioning)
        assert len(result) == 64
