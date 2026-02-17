"""Unit tests for type-level validation.

Tests the validation methods on all domain types:
- FileMetadata
- FilePathModel
- FileMetadataModel
- ContentChunkModel
"""

from datetime import UTC, datetime

import pytest

from nexus.core.exceptions import ValidationError
from nexus.core.metadata import FileMetadata
from nexus.storage.models import ContentChunkModel, FileMetadataModel, FilePathModel


class TestFileMetadataValidation:
    """Test suite for FileMetadata validation."""

    def test_valid_metadata(self):
        """Test that valid metadata passes validation."""
        metadata = FileMetadata(
            path="/data/file.txt",
            backend_name="local",
            physical_path="/storage/file.txt",
            size=1024,
        )
        # Should not raise
        metadata.validate()

    def test_path_required(self):
        """Test that path is required."""
        metadata = FileMetadata(
            path="",
            backend_name="local",
            physical_path="/storage/file.txt",
            size=1024,
        )
        with pytest.raises(ValidationError, match="path is required"):
            metadata.validate()

    def test_path_must_start_with_slash(self):
        """Test that path must start with /."""
        metadata = FileMetadata(
            path="data/file.txt",
            backend_name="local",
            physical_path="/storage/file.txt",
            size=1024,
        )
        with pytest.raises(ValidationError, match="path must start with '/'"):
            metadata.validate()

    def test_path_cannot_contain_null_bytes(self):
        """Test that path cannot contain null bytes."""
        metadata = FileMetadata(
            path="/data/file\x00.txt",
            backend_name="local",
            physical_path="/storage/file.txt",
            size=1024,
        )
        with pytest.raises(ValidationError, match="path contains null bytes"):
            metadata.validate()

    def test_backend_name_required(self):
        """Test that backend_name is required."""
        metadata = FileMetadata(
            path="/data/file.txt",
            backend_name="",
            physical_path="/storage/file.txt",
            size=1024,
        )
        with pytest.raises(ValidationError, match="backend_name is required"):
            metadata.validate()

    def test_physical_path_required(self):
        """Test that physical_path is required."""
        metadata = FileMetadata(
            path="/data/file.txt",
            backend_name="local",
            physical_path="",
            size=1024,
        )
        with pytest.raises(ValidationError, match="physical_path is required"):
            metadata.validate()

    def test_size_cannot_be_negative(self):
        """Test that size cannot be negative."""
        metadata = FileMetadata(
            path="/data/file.txt",
            backend_name="local",
            physical_path="/storage/file.txt",
            size=-100,
        )
        with pytest.raises(ValidationError, match="size cannot be negative"):
            metadata.validate()

    def test_version_must_be_at_least_one(self):
        """Test that version must be >= 1."""
        metadata = FileMetadata(
            path="/data/file.txt",
            backend_name="local",
            physical_path="/storage/file.txt",
            size=1024,
            version=0,
        )
        with pytest.raises(ValidationError, match="version must be >= 1"):
            metadata.validate()


class TestFilePathModelValidation:
    """Test suite for FilePathModel validation."""

    def test_valid_file_path_model(self):
        """Test that valid FilePathModel passes validation."""
        file_path = FilePathModel(
            virtual_path="/data/file.txt",
            backend_id="local",
            physical_path="/storage/file.txt",
            size_bytes=1024,
        )
        # Should not raise
        file_path.validate()

    def test_virtual_path_required(self):
        """Test that virtual_path is required."""
        file_path = FilePathModel(
            virtual_path="", backend_id="local", physical_path="/storage/file.txt", size_bytes=1024
        )
        with pytest.raises(ValidationError, match="virtual_path is required"):
            file_path.validate()

    def test_virtual_path_must_start_with_slash(self):
        """Test that virtual_path must start with /."""
        file_path = FilePathModel(
            virtual_path="data/file.txt",
            backend_id="local",
            physical_path="/storage/file.txt",
            size_bytes=1024,
        )
        with pytest.raises(ValidationError, match="virtual_path must start with '/'"):
            file_path.validate()

    def test_virtual_path_cannot_contain_null_bytes(self):
        """Test that virtual_path cannot contain null bytes."""
        file_path = FilePathModel(
            virtual_path="/data/file\x00.txt",
            backend_id="local",
            physical_path="/storage/file.txt",
            size_bytes=1024,
        )
        with pytest.raises(ValidationError, match="virtual_path contains null bytes"):
            file_path.validate()

    def test_backend_id_required(self):
        """Test that backend_id is required."""
        file_path = FilePathModel(
            virtual_path="/data/file.txt",
            backend_id="",
            physical_path="/storage/file.txt",
            size_bytes=1024,
        )
        with pytest.raises(ValidationError, match="backend_id is required"):
            file_path.validate()

    def test_physical_path_required(self):
        """Test that physical_path is required."""
        file_path = FilePathModel(
            virtual_path="/data/file.txt", backend_id="local", physical_path="", size_bytes=1024
        )
        with pytest.raises(ValidationError, match="physical_path is required"):
            file_path.validate()

    def test_size_bytes_cannot_be_negative(self):
        """Test that size_bytes cannot be negative."""
        file_path = FilePathModel(
            virtual_path="/data/file.txt",
            backend_id="local",
            physical_path="/storage/file.txt",
            size_bytes=-100,
        )
        with pytest.raises(ValidationError, match="size_bytes cannot be negative"):
            file_path.validate()


class TestFileMetadataModelValidation:
    """Test suite for FileMetadataModel validation."""

    def test_valid_file_metadata_model(self):
        """Test that valid FileMetadataModel passes validation."""
        metadata = FileMetadataModel(
            path_id="test-path-id",
            key="author",
            value='"John Doe"',
            created_at=datetime.now(UTC),
        )
        # Should not raise
        metadata.validate()

    def test_path_id_required(self):
        """Test that path_id is required."""
        metadata = FileMetadataModel(
            path_id="",
            key="author",
            value='"John Doe"',
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError, match="path_id is required"):
            metadata.validate()

    def test_key_required(self):
        """Test that key is required."""
        metadata = FileMetadataModel(
            path_id="test-path-id",
            key="",
            value='"John Doe"',
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError, match="metadata key is required"):
            metadata.validate()

    def test_key_max_length(self):
        """Test that key must be <= 255 characters."""
        metadata = FileMetadataModel(
            path_id="test-path-id",
            key="a" * 256,
            value='"test"',
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError, match="metadata key must be 255 characters or less"):
            metadata.validate()


class TestContentChunkModelValidation:
    """Test suite for ContentChunkModel validation."""

    def test_valid_content_chunk_model(self):
        """Test that valid ContentChunkModel passes validation."""
        chunk = ContentChunkModel(
            content_hash="a" * 64,
            size_bytes=1024,
            storage_path="/storage/chunks/abc",
            ref_count=1,
        )
        # Should not raise
        chunk.validate()

    def test_content_hash_required(self):
        """Test that content_hash is required."""
        chunk = ContentChunkModel(
            content_hash="",
            size_bytes=1024,
            storage_path="/storage/chunks/abc",
            ref_count=1,
        )
        with pytest.raises(ValidationError, match="content_hash is required"):
            chunk.validate()

    def test_content_hash_length(self):
        """Test that content_hash must be 64 characters."""
        chunk = ContentChunkModel(
            content_hash="tooshort",
            size_bytes=1024,
            storage_path="/storage/chunks/abc",
            ref_count=1,
        )
        with pytest.raises(ValidationError, match="content_hash must be 64 characters"):
            chunk.validate()

    def test_content_hash_hex_only(self):
        """Test that content_hash must contain only hex characters."""
        chunk = ContentChunkModel(
            content_hash="z" * 64,  # Invalid hex
            size_bytes=1024,
            storage_path="/storage/chunks/abc",
            ref_count=1,
        )
        with pytest.raises(ValidationError, match="content_hash must contain only hexadecimal"):
            chunk.validate()

    def test_size_bytes_cannot_be_negative(self):
        """Test that size_bytes cannot be negative."""
        chunk = ContentChunkModel(
            content_hash="a" * 64,
            size_bytes=-100,
            storage_path="/storage/chunks/abc",
            ref_count=1,
        )
        with pytest.raises(ValidationError, match="size_bytes cannot be negative"):
            chunk.validate()

    def test_storage_path_required(self):
        """Test that storage_path is required."""
        chunk = ContentChunkModel(
            content_hash="a" * 64,
            size_bytes=1024,
            storage_path="",
            ref_count=1,
        )
        with pytest.raises(ValidationError, match="storage_path is required"):
            chunk.validate()

    def test_ref_count_cannot_be_negative(self):
        """Test that ref_count cannot be negative."""
        chunk = ContentChunkModel(
            content_hash="a" * 64,
            size_bytes=1024,
            storage_path="/storage/chunks/abc",
            ref_count=-1,
        )
        with pytest.raises(ValidationError, match="ref_count cannot be negative"):
            chunk.validate()


class TestTableDrivenValidation:
    """Table-driven validation tests for comprehensive coverage."""

    @pytest.mark.parametrize(
        "path,size,should_fail,error_match",
        [
            # Valid cases
            ("/data/file.txt", 0, False, None),
            ("/data/file.txt", 1024, False, None),
            ("/data/nested/dir/file.txt", 9999, False, None),
            # Invalid paths
            ("relative/path", 100, True, "path must start with '/'"),
            ("", 100, True, "path is required"),
            ("/data/file\x00.txt", 100, True, "path contains null bytes"),
            # Invalid sizes
            ("/data/file.txt", -1, True, "size cannot be negative"),
            ("/data/file.txt", -1000, True, "size cannot be negative"),
        ],
    )
    def test_file_metadata_validation_table(self, path, size, should_fail, error_match):
        """Table-driven test for FileMetadata validation."""
        metadata = FileMetadata(
            path=path,
            backend_name="local",
            physical_path="/storage/file.txt",
            size=size,
        )

        if should_fail:
            with pytest.raises(ValidationError, match=error_match):
                metadata.validate()
        else:
            # Should not raise
            metadata.validate()

    @pytest.mark.parametrize(
        "content_hash,size_bytes,ref_count,should_fail,error_match",
        [
            # Valid cases
            ("a" * 64, 0, 0, False, None),
            ("0" * 64, 1024, 1, False, None),
            ("f" * 64, 9999, 100, False, None),
            # Invalid hash length
            ("tooshort", 100, 1, True, "content_hash must be 64 characters"),
            ("a" * 63, 100, 1, True, "content_hash must be 64 characters"),
            ("a" * 65, 100, 1, True, "content_hash must be 64 characters"),
            # Invalid hash characters
            ("z" * 64, 100, 1, True, "content_hash must contain only hexadecimal"),
            ("x" * 64, 100, 1, True, "content_hash must contain only hexadecimal"),
            # Invalid sizes and counts
            ("a" * 64, -1, 1, True, "size_bytes cannot be negative"),
            ("a" * 64, 100, -1, True, "ref_count cannot be negative"),
        ],
    )
    def test_content_chunk_validation_table(
        self, content_hash, size_bytes, ref_count, should_fail, error_match
    ):
        """Table-driven test for ContentChunkModel validation."""
        chunk = ContentChunkModel(
            content_hash=content_hash,
            size_bytes=size_bytes,
            storage_path="/storage/chunks/test",
            ref_count=ref_count,
        )

        if should_fail:
            with pytest.raises(ValidationError, match=error_match):
                chunk.validate()
        else:
            # Should not raise
            chunk.validate()
