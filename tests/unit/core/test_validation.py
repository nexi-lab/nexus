"""Unit tests for type-level validation.

Tests the validation methods on all domain types:
- FileMetadata
- FilePathModel
- FileMetadataModel
"""

from datetime import UTC, datetime

import pytest

from nexus.contracts.exceptions import ValidationError
from nexus.contracts.metadata import FileMetadata
from nexus.storage.models import FileMetadataModel, FilePathModel


class TestFileMetadataValidation:
    """Test suite for FileMetadata validation."""

    def test_valid_metadata(self):
        """Test that valid metadata passes validation."""
        metadata = FileMetadata(
            path="/data/file.txt",
            size=1024,
        )
        # Should not raise
        metadata.validate()

    def test_path_required(self):
        """Test that path is required."""
        metadata = FileMetadata(
            path="",
            size=1024,
        )
        with pytest.raises(ValidationError, match="path is required"):
            metadata.validate()

    def test_path_must_start_with_slash(self):
        """Test that path must start with /."""
        metadata = FileMetadata(
            path="data/file.txt",
            size=1024,
        )
        with pytest.raises(ValidationError, match="path must start with '/'"):
            metadata.validate()

    def test_path_cannot_contain_null_bytes(self):
        """Test that path cannot contain null bytes."""
        metadata = FileMetadata(
            path="/data/file\x00.txt",
            size=1024,
        )
        with pytest.raises(ValidationError, match="path contains null bytes"):
            metadata.validate()

    def test_size_cannot_be_negative(self):
        """Test that size cannot be negative."""
        metadata = FileMetadata(
            path="/data/file.txt",
            size=-100,
        )
        with pytest.raises(ValidationError, match="size cannot be negative"):
            metadata.validate()

    def test_version_must_be_at_least_one(self):
        """Test that version must be >= 1."""
        metadata = FileMetadata(
            path="/data/file.txt",
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
            size_bytes=1024,
        )
        # Should not raise
        file_path.validate()

    def test_virtual_path_required(self):
        """Test that virtual_path is required."""
        file_path = FilePathModel(virtual_path="", size_bytes=1024)
        with pytest.raises(ValidationError, match="virtual_path is required"):
            file_path.validate()

    def test_virtual_path_must_start_with_slash(self):
        """Test that virtual_path must start with /."""
        file_path = FilePathModel(virtual_path="data/file.txt", size_bytes=1024)
        with pytest.raises(ValidationError, match="virtual_path must start with '/'"):
            file_path.validate()

    def test_virtual_path_cannot_contain_null_bytes(self):
        """Test that virtual_path cannot contain null bytes."""
        file_path = FilePathModel(virtual_path="/data/file\x00.txt", size_bytes=1024)
        with pytest.raises(ValidationError, match="virtual_path contains null bytes"):
            file_path.validate()

    def test_size_bytes_cannot_be_negative(self):
        """Test that size_bytes cannot be negative."""
        file_path = FilePathModel(virtual_path="/data/file.txt", size_bytes=-100)
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
            size=size,
        )

        if should_fail:
            with pytest.raises(ValidationError, match=error_match):
                metadata.validate()
        else:
            # Should not raise
            metadata.validate()
