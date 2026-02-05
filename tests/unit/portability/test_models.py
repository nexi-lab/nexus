"""Unit tests for portability models.

Tests cover:
- ExportManifest serialization/deserialization
- ZoneExportOptions validation
- ZoneImportOptions remapping logic
- ImportResult tracking
- FileRecord and PermissionRecord JSONL handling
- BundleChecksums integrity verification
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.portability import (
    BUNDLE_FORMAT_VERSION,
    BUNDLE_PATHS,
    MANIFEST_SCHEMA_PATH,
    MANIFEST_SCHEMA_URL,
    BundleChecksums,
    ConflictMode,
    ContentMode,
    ExportManifest,
    FileChecksum,
    FileRecord,
    ImportResult,
    PermissionRecord,
    ZoneExportOptions,
    ZoneImportOptions,
)

# =============================================================================
# FileChecksum Tests
# =============================================================================


class TestFileChecksum:
    """Tests for FileChecksum dataclass."""

    def test_create_checksum(self):
        """Test creating a FileChecksum."""
        checksum = FileChecksum(
            path="metadata/files.jsonl",
            algorithm="sha256",
            hash="abc123def456",
            size_bytes=1024,
        )
        assert checksum.path == "metadata/files.jsonl"
        assert checksum.algorithm == "sha256"
        assert checksum.hash == "abc123def456"
        assert checksum.size_bytes == 1024

    def test_verify_sha256(self):
        """Test SHA-256 verification."""
        data = b"Hello, World!"
        # SHA-256 of "Hello, World!"
        expected_hash = "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"

        checksum = FileChecksum(
            path="test.txt",
            algorithm="sha256",
            hash=expected_hash,
            size_bytes=len(data),
        )

        assert checksum.verify(data) is True
        assert checksum.verify(b"Wrong data") is False

    def test_verify_unsupported_algorithm(self):
        """Test verification with unsupported algorithm raises error."""
        checksum = FileChecksum(
            path="test.txt",
            algorithm="unsupported",
            hash="abc",
            size_bytes=10,
        )

        with pytest.raises(ValueError, match="Unsupported hash algorithm"):
            checksum.verify(b"data")

    def test_to_dict(self):
        """Test conversion to dictionary."""
        checksum = FileChecksum(
            path="test.txt",
            algorithm="sha256",
            hash="abc123",
            size_bytes=100,
        )

        result = checksum.to_dict()

        assert result == {
            "path": "test.txt",
            "algorithm": "sha256",
            "hash": "abc123",
            "size_bytes": 100,
        }

    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "path": "test.txt",
            "algorithm": "sha256",
            "hash": "abc123",
            "size_bytes": 100,
        }

        checksum = FileChecksum.from_dict(data)

        assert checksum.path == "test.txt"
        assert checksum.algorithm == "sha256"
        assert checksum.hash == "abc123"
        assert checksum.size_bytes == 100


# =============================================================================
# BundleChecksums Tests
# =============================================================================


class TestBundleChecksums:
    """Tests for BundleChecksums dataclass."""

    def test_default_algorithm(self):
        """Test default hash algorithm is sha256."""
        checksums = BundleChecksums()
        assert checksums.algorithm == "sha256"

    def test_add_file(self):
        """Test adding a file computes its checksum."""
        checksums = BundleChecksums()
        data = b"test content"

        result = checksums.add_file("test.txt", data)

        assert result.path == "test.txt"
        assert result.algorithm == "sha256"
        assert result.size_bytes == len(data)
        assert "test.txt" in checksums.files

    def test_verify_file(self):
        """Test file verification."""
        checksums = BundleChecksums()
        data = b"test content"
        checksums.add_file("test.txt", data)

        assert checksums.verify_file("test.txt", data) is True
        assert checksums.verify_file("test.txt", b"wrong content") is False
        assert checksums.verify_file("nonexistent.txt", data) is False

    def test_to_dict_from_dict_roundtrip(self):
        """Test serialization/deserialization roundtrip."""
        checksums = BundleChecksums(merkle_root="root123")
        checksums.add_file("file1.txt", b"content1")
        checksums.add_file("file2.txt", b"content2")

        data = checksums.to_dict()
        restored = BundleChecksums.from_dict(data)

        assert restored.algorithm == checksums.algorithm
        assert restored.merkle_root == checksums.merkle_root
        assert len(restored.files) == 2
        assert "file1.txt" in restored.files
        assert "file2.txt" in restored.files

    def test_compute_merkle_root(self):
        """Test Merkle root computation."""
        checksums = BundleChecksums()
        checksums.add_file("a.txt", b"content a")
        checksums.add_file("b.txt", b"content b")
        checksums.add_file("c.txt", b"content c")

        root = checksums.compute_merkle_root()

        # Should be 64 hex characters (SHA-256)
        assert len(root) == 64
        assert all(c in "0123456789abcdef" for c in root)
        assert checksums.merkle_root == root

    def test_compute_merkle_root_empty(self):
        """Test Merkle root with no files."""
        checksums = BundleChecksums()

        root = checksums.compute_merkle_root()

        # SHA-256 of empty string
        import hashlib

        expected = hashlib.sha256(b"").hexdigest()
        assert root == expected

    def test_compute_merkle_root_deterministic(self):
        """Test Merkle root is deterministic regardless of add order."""
        checksums1 = BundleChecksums()
        checksums1.add_file("a.txt", b"content a")
        checksums1.add_file("b.txt", b"content b")
        root1 = checksums1.compute_merkle_root()

        # Add in different order
        checksums2 = BundleChecksums()
        checksums2.add_file("b.txt", b"content b")
        checksums2.add_file("a.txt", b"content a")
        root2 = checksums2.compute_merkle_root()

        assert root1 == root2

    def test_verify_merkle_root(self):
        """Test Merkle root verification."""
        checksums = BundleChecksums()
        checksums.add_file("a.txt", b"content a")
        checksums.add_file("b.txt", b"content b")
        checksums.compute_merkle_root()

        assert checksums.verify_merkle_root() is True

        # Corrupt the root
        checksums.merkle_root = "invalid_root"
        assert checksums.verify_merkle_root() is False

    def test_verify_merkle_root_none(self):
        """Test verification when no Merkle root is set."""
        checksums = BundleChecksums()
        checksums.add_file("a.txt", b"content a")

        # No Merkle root computed yet
        assert checksums.verify_merkle_root() is False


# =============================================================================
# ZoneExportOptions Tests
# =============================================================================


class TestZoneExportOptions:
    """Tests for ZoneExportOptions dataclass."""

    def test_default_values(self):
        """Test default option values."""
        options = ZoneExportOptions(output_path=Path("/backup/export.nexus"))

        assert options.include_content is True
        assert options.include_permissions is True
        assert options.include_embeddings is False
        assert options.include_api_keys is False
        assert options.include_deleted is False
        assert options.include_versions is True
        assert options.compression_level == 6
        assert options.max_concurrent_reads == 10

    def test_string_path_converted(self):
        """Test that string paths are converted to Path objects."""
        options = ZoneExportOptions(output_path="/backup/export.nexus")  # type: ignore
        assert isinstance(options.output_path, Path)

    def test_invalid_compression_level(self):
        """Test validation of compression level."""
        with pytest.raises(ValueError, match="compression_level must be 1-9"):
            ZoneExportOptions(
                output_path=Path("/backup/export.nexus"),
                compression_level=10,
            )

        with pytest.raises(ValueError, match="compression_level must be 1-9"):
            ZoneExportOptions(
                output_path=Path("/backup/export.nexus"),
                compression_level=0,
            )

    def test_invalid_concurrent_reads(self):
        """Test validation of max_concurrent_reads."""
        with pytest.raises(ValueError, match="max_concurrent_reads must be >= 1"):
            ZoneExportOptions(
                output_path=Path("/backup/export.nexus"),
                max_concurrent_reads=0,
            )

    def test_api_keys_require_encryption_key(self):
        """Test that API key export requires encryption key."""
        with pytest.raises(ValueError, match="encryption_key required"):
            ZoneExportOptions(
                output_path=Path("/backup/export.nexus"),
                include_api_keys=True,
                encryption_key=None,
            )

        # Should work with encryption key
        options = ZoneExportOptions(
            output_path=Path("/backup/export.nexus"),
            include_api_keys=True,
            encryption_key=b"secret-key-123456",
        )
        assert options.include_api_keys is True

    def test_to_dict_excludes_encryption_key(self):
        """Test that to_dict excludes sensitive encryption_key."""
        options = ZoneExportOptions(
            output_path=Path("/backup/export.nexus"),
            include_api_keys=True,
            encryption_key=b"secret",
        )

        result = options.to_dict()

        assert "encryption_key" not in result
        assert result["include_api_keys"] is True


# =============================================================================
# ZoneImportOptions Tests
# =============================================================================


class TestZoneImportOptions:
    """Tests for ZoneImportOptions dataclass."""

    def test_default_values(self):
        """Test default option values."""
        options = ZoneImportOptions(bundle_path=Path("/backup/import.nexus"))

        assert options.target_zone_id is None
        assert options.conflict_mode == ConflictMode.SKIP
        assert options.preserve_timestamps is True
        assert options.preserve_ids is False
        assert options.dry_run is False
        assert options.content_mode == ContentMode.INCLUDE
        assert options.import_permissions is True
        assert options.import_api_keys is False

    def test_string_conflict_mode_converted(self):
        """Test that string conflict mode is converted to enum."""
        options = ZoneImportOptions(
            bundle_path=Path("/backup/import.nexus"),
            conflict_mode="overwrite",  # type: ignore
        )
        assert options.conflict_mode == ConflictMode.OVERWRITE

    def test_string_content_mode_converted(self):
        """Test that string content mode is converted to enum."""
        options = ZoneImportOptions(
            bundle_path=Path("/backup/import.nexus"),
            content_mode="reference",  # type: ignore
        )
        assert options.content_mode == ContentMode.REFERENCE

    def test_api_keys_require_decryption_key(self):
        """Test that API key import requires decryption key."""
        with pytest.raises(ValueError, match="decryption_key required"):
            ZoneImportOptions(
                bundle_path=Path("/backup/import.nexus"),
                import_api_keys=True,
                decryption_key=None,
            )

    def test_remap_path(self):
        """Test path prefix remapping."""
        options = ZoneImportOptions(
            bundle_path=Path("/backup/import.nexus"),
            path_prefix_remap={
                "/companyA/": "/companyB/",
                "/old/": "/new/",
            },
        )

        assert options.remap_path("/companyA/docs/readme.md") == "/companyB/docs/readme.md"
        assert options.remap_path("/old/file.txt") == "/new/file.txt"
        assert options.remap_path("/other/file.txt") == "/other/file.txt"  # No change

    def test_remap_user(self):
        """Test user ID remapping."""
        options = ZoneImportOptions(
            bundle_path=Path("/backup/import.nexus"),
            user_id_remap={
                "old-user-1": "new-user-1",
                "old-user-2": "new-user-2",
            },
        )

        assert options.remap_user("old-user-1") == "new-user-1"
        assert options.remap_user("old-user-2") == "new-user-2"
        assert options.remap_user("unknown-user") == "unknown-user"  # No change

    def test_to_dict_excludes_decryption_key(self):
        """Test that to_dict excludes sensitive decryption_key."""
        options = ZoneImportOptions(
            bundle_path=Path("/backup/import.nexus"),
            import_api_keys=True,
            decryption_key=b"secret",
        )

        result = options.to_dict()

        assert "decryption_key" not in result


# =============================================================================
# ExportManifest Tests
# =============================================================================


class TestExportManifest:
    """Tests for ExportManifest dataclass."""

    def test_default_values(self):
        """Test default manifest values."""
        manifest = ExportManifest()

        assert manifest.format_version == BUNDLE_FORMAT_VERSION
        assert manifest.nexus_version == ""
        assert manifest.bundle_id  # Should have a UUID
        assert manifest.file_count == 0
        assert manifest.include_content is True
        assert manifest.include_permissions is True
        assert manifest.include_embeddings is False

    def test_to_dict_structure(self):
        """Test manifest to_dict produces correct structure."""
        manifest = ExportManifest(
            nexus_version="0.8.0",
            source_instance="https://nexus.company.com",
            source_zone_id="zone-123",
            file_count=100,
            total_size_bytes=1024000,
            content_blob_count=50,
            permission_count=200,
        )

        result = manifest.to_dict()

        # Check structure
        assert "$schema" in result
        assert result["format_version"] == BUNDLE_FORMAT_VERSION
        assert result["nexus_version"] == "0.8.0"
        assert result["source_instance"] == "https://nexus.company.com"
        assert result["source_zone_id"] == "zone-123"

        # Check statistics
        assert result["statistics"]["file_count"] == 100
        assert result["statistics"]["total_size_bytes"] == 1024000
        assert result["statistics"]["content_blob_count"] == 50
        assert result["statistics"]["permission_count"] == 200

        # Check options
        assert result["options"]["include_content"] is True
        assert result["options"]["include_permissions"] is True

    def test_to_json_from_json_roundtrip(self):
        """Test JSON serialization/deserialization roundtrip."""
        original = ExportManifest(
            nexus_version="0.8.0",
            source_instance="https://nexus.company.com",
            source_zone_id="zone-123",
            file_count=100,
            total_size_bytes=1024000,
            include_content=True,
            include_embeddings=True,
            path_prefix_filter="/workspace/",
            after_time_filter=datetime(2025, 1, 1, tzinfo=UTC),
        )

        json_str = original.to_json()
        restored = ExportManifest.from_json(json_str)

        assert restored.format_version == original.format_version
        assert restored.nexus_version == original.nexus_version
        assert restored.source_instance == original.source_instance
        assert restored.source_zone_id == original.source_zone_id
        assert restored.file_count == original.file_count
        assert restored.total_size_bytes == original.total_size_bytes
        assert restored.include_content == original.include_content
        assert restored.include_embeddings == original.include_embeddings
        assert restored.path_prefix_filter == original.path_prefix_filter
        # Note: datetime comparison might differ in timezone representation

    def test_from_dict_with_minimal_data(self):
        """Test from_dict handles minimal data gracefully."""
        data = {
            "format_version": "1.0.0",
            "source_zone_id": "zone-123",
        }

        manifest = ExportManifest.from_dict(data)

        assert manifest.format_version == "1.0.0"
        assert manifest.source_zone_id == "zone-123"
        assert manifest.file_count == 0  # Default
        assert manifest.include_content is True  # Default

    def test_validate_valid_manifest(self):
        """Test validation of valid manifest."""
        manifest = ExportManifest(
            nexus_version="0.8.0",
            source_zone_id="zone-123",
            file_count=10,
        )

        errors = manifest.validate()

        assert len(errors) == 0

    def test_validate_missing_required_fields(self):
        """Test validation catches missing required fields."""
        manifest = ExportManifest(
            format_version="",
            source_zone_id="",
            bundle_id="",
        )

        errors = manifest.validate()

        assert "format_version is required" in errors
        assert "bundle_id is required" in errors
        assert "source_zone_id is required" in errors

    def test_validate_negative_counts(self):
        """Test validation catches negative counts."""
        manifest = ExportManifest(
            source_zone_id="zone-123",
            file_count=-1,
            total_size_bytes=-100,
        )

        errors = manifest.validate()

        assert "file_count cannot be negative" in errors
        assert "total_size_bytes cannot be negative" in errors

    def test_encryption_fields(self):
        """Test encryption metadata in manifest."""
        manifest = ExportManifest(
            source_zone_id="zone-123",
            encryption_method="age-v1",
            encrypted_dek="base64-encoded-key",
        )

        result = manifest.to_dict()

        assert result["encryption"]["method"] == "age-v1"
        assert result["encryption"]["encrypted_dek"] == "base64-encoded-key"

    def test_get_schema(self):
        """Test loading JSON schema."""
        schema = ExportManifest.get_schema()

        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["title"] == "Nexus Export Bundle Manifest"
        assert "properties" in schema
        assert "format_version" in schema["properties"]
        assert "statistics" in schema["properties"]

    def test_validate_against_schema_valid(self):
        """Test schema validation with valid manifest."""
        pytest.importorskip("jsonschema")

        manifest = ExportManifest(
            nexus_version="0.8.0",
            source_instance="https://nexus.company.com",
            source_zone_id="zone-123",
            file_count=100,
            total_size_bytes=1024000,
            content_blob_count=50,
            permission_count=200,
        )

        errors = manifest.validate_against_schema()

        assert len(errors) == 0, f"Unexpected validation errors: {errors}"

    def test_validate_against_schema_missing_required(self):
        """Test schema validation catches missing required fields."""
        pytest.importorskip("jsonschema")

        manifest = ExportManifest(
            format_version="",  # Invalid empty string
            bundle_id="",  # Invalid empty string
            source_zone_id="zone-123",
        )

        errors = manifest.validate_against_schema()

        # Should have errors for empty format_version and bundle_id
        assert len(errors) > 0


# =============================================================================
# ImportResult Tests
# =============================================================================


class TestImportResult:
    """Tests for ImportResult dataclass."""

    def test_default_values(self):
        """Test default result values."""
        result = ImportResult()

        assert result.files_created == 0
        assert result.files_updated == 0
        assert result.files_skipped == 0
        assert result.files_failed == 0
        assert result.success is True
        assert result.total_files_processed == 0

    def test_total_files_processed(self):
        """Test total_files_processed calculation."""
        result = ImportResult(
            files_created=10,
            files_updated=5,
            files_skipped=3,
            files_failed=2,
        )

        assert result.total_files_processed == 20

    def test_success_property(self):
        """Test success property based on errors."""
        result = ImportResult()
        assert result.success is True

        result.add_error(
            path="/test/file.txt",
            error_type="validation",
            message="Invalid format",
        )
        assert result.success is False

    def test_add_error(self):
        """Test adding errors."""
        result = ImportResult()

        result.add_error(
            path="/test/file.txt",
            error_type="conflict",
            message="File already exists",
            details={"existing_hash": "abc123"},
        )

        assert len(result.errors) == 1
        assert result.errors[0].path == "/test/file.txt"
        assert result.errors[0].error_type == "conflict"
        assert result.errors[0].details["existing_hash"] == "abc123"

    def test_add_warning(self):
        """Test adding warnings."""
        result = ImportResult()

        result.add_warning("Some files were skipped")
        result.add_warning("Permissions may need review")

        assert len(result.warnings) == 2
        assert "skipped" in result.warnings[0]

    def test_duration_seconds(self):
        """Test duration calculation."""
        result = ImportResult(
            started_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            completed_at=datetime(2025, 1, 1, 12, 0, 30, tzinfo=UTC),
        )

        assert result.duration_seconds == 30.0

    def test_duration_seconds_not_completed(self):
        """Test duration when not completed."""
        result = ImportResult(started_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC))

        assert result.duration_seconds == 0.0

    def test_to_dict(self):
        """Test conversion to dictionary."""
        result = ImportResult(
            files_created=10,
            files_updated=5,
            permissions_imported=20,
            content_blobs_imported=15,
            zone_remapped=True,
            paths_remapped=8,
            started_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            completed_at=datetime(2025, 1, 1, 12, 1, 0, tzinfo=UTC),
        )
        result.add_warning("Test warning")

        data = result.to_dict()

        assert data["files"]["created"] == 10
        assert data["files"]["updated"] == 5
        assert data["files"]["total_processed"] == 15
        assert data["permissions"]["imported"] == 20
        assert data["content"]["blobs_imported"] == 15
        assert data["remapping"]["zone_remapped"] is True
        assert data["remapping"]["paths_remapped"] == 8
        assert data["timing"]["duration_seconds"] == 60.0
        assert data["success"] is True
        assert len(data["warnings"]) == 1

    def test_str_representation(self):
        """Test string representation."""
        result = ImportResult(
            files_created=10,
            files_updated=5,
            files_skipped=2,
            files_failed=0,
            started_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            completed_at=datetime(2025, 1, 1, 12, 0, 30, tzinfo=UTC),
        )

        str_repr = str(result)

        assert "SUCCESS" in str_repr
        assert "created=10" in str_repr
        assert "updated=5" in str_repr


# =============================================================================
# FileRecord Tests
# =============================================================================


class TestFileRecord:
    """Tests for FileRecord dataclass."""

    def test_create_file_record(self):
        """Test creating a file record."""
        record = FileRecord(
            path_id="uuid-123",
            zone_id="zone-abc",
            virtual_path="/docs/readme.md",
            backend_id="local-1",
            physical_path="/data/abc123",
            file_type="text/markdown",
            size_bytes=1024,
            content_hash="sha256:abc123",
        )

        assert record.path_id == "uuid-123"
        assert record.virtual_path == "/docs/readme.md"
        assert record.size_bytes == 1024

    def test_to_jsonl_from_jsonl_roundtrip(self):
        """Test JSONL serialization roundtrip."""
        original = FileRecord(
            path_id="uuid-123",
            zone_id="zone-abc",
            virtual_path="/docs/readme.md",
            backend_id="local-1",
            physical_path="/data/abc123",
            file_type="text/markdown",
            size_bytes=1024,
            content_hash="sha256:abc123",
            created_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            updated_at=datetime(2025, 1, 15, 14, 30, 0, tzinfo=UTC),
            current_version=3,
            metadata={"custom": "value"},
        )

        jsonl_line = original.to_jsonl()
        restored = FileRecord.from_jsonl(jsonl_line)

        assert restored.path_id == original.path_id
        assert restored.zone_id == original.zone_id
        assert restored.virtual_path == original.virtual_path
        assert restored.backend_id == original.backend_id
        assert restored.physical_path == original.physical_path
        assert restored.file_type == original.file_type
        assert restored.size_bytes == original.size_bytes
        assert restored.content_hash == original.content_hash
        assert restored.current_version == original.current_version
        assert restored.metadata == original.metadata

    def test_jsonl_is_single_line(self):
        """Test that JSONL output is a single line."""
        record = FileRecord(
            path_id="uuid-123",
            zone_id="zone-abc",
            virtual_path="/docs/readme.md",
            backend_id="local-1",
            physical_path="/data/abc123",
        )

        jsonl_line = record.to_jsonl()

        assert "\n" not in jsonl_line
        assert json.loads(jsonl_line)  # Should be valid JSON


# =============================================================================
# PermissionRecord Tests
# =============================================================================


class TestPermissionRecord:
    """Tests for PermissionRecord dataclass."""

    def test_create_permission_record(self):
        """Test creating a permission record."""
        record = PermissionRecord(
            object_type="file",
            object_id="file-uuid-123",
            relation="owner",
            subject_type="user",
            subject_id="user-uuid-456",
        )

        assert record.object_type == "file"
        assert record.relation == "owner"
        assert record.subject_type == "user"

    def test_to_jsonl_from_jsonl_roundtrip(self):
        """Test JSONL serialization roundtrip."""
        original = PermissionRecord(
            object_type="directory",
            object_id="dir-uuid-123",
            relation="editor",
            subject_type="group",
            subject_id="group-uuid-789",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        jsonl_line = original.to_jsonl()
        restored = PermissionRecord.from_jsonl(jsonl_line)

        assert restored.object_type == original.object_type
        assert restored.object_id == original.object_id
        assert restored.relation == original.relation
        assert restored.subject_type == original.subject_type
        assert restored.subject_id == original.subject_id


# =============================================================================
# Constants Tests
# =============================================================================


class TestBundlePaths:
    """Tests for bundle path constants."""

    def test_required_paths_exist(self):
        """Test that all required bundle paths are defined."""
        required_paths = [
            "manifest",
            "files",
            "versions",
            "permissions",
            "content",
        ]

        for path_key in required_paths:
            assert path_key in BUNDLE_PATHS, f"Missing required path: {path_key}"

    def test_path_values(self):
        """Test specific path values."""
        assert BUNDLE_PATHS["manifest"] == "manifest.json"
        assert BUNDLE_PATHS["files"] == "metadata/files.jsonl"
        assert BUNDLE_PATHS["permissions"] == "permissions/rebac_tuples.jsonl"
        assert BUNDLE_PATHS["embeddings"] == "embeddings/vectors.parquet"
        assert BUNDLE_PATHS["content"] == "content/cas"


# =============================================================================
# Schema Constants Tests
# =============================================================================


class TestSchemaConstants:
    """Tests for JSON Schema constants."""

    def test_schema_url(self):
        """Test schema URL is correctly defined."""
        assert MANIFEST_SCHEMA_URL == "https://nexus.io/schemas/manifest-v1.json"

    def test_schema_path_exists(self):
        """Test schema file exists at expected path."""
        assert MANIFEST_SCHEMA_PATH.exists(), f"Schema file not found: {MANIFEST_SCHEMA_PATH}"

    def test_schema_is_valid_json(self):
        """Test schema file contains valid JSON."""
        schema_content = MANIFEST_SCHEMA_PATH.read_text()
        schema = json.loads(schema_content)

        assert "$schema" in schema
        assert "title" in schema
        assert "properties" in schema

    def test_schema_has_required_fields(self):
        """Test schema defines all required manifest fields."""
        schema = json.loads(MANIFEST_SCHEMA_PATH.read_text())

        required_properties = [
            "format_version",
            "bundle_id",
            "source_zone_id",
            "export_timestamp",
            "statistics",
            "options",
            "checksums",
        ]

        for prop in required_properties:
            assert prop in schema["properties"], f"Missing property: {prop}"

    def test_schema_defs(self):
        """Test schema contains FileChecksum definition."""
        schema = json.loads(MANIFEST_SCHEMA_PATH.read_text())

        assert "$defs" in schema
        assert "FileChecksum" in schema["$defs"]


# =============================================================================
# ConflictMode and ContentMode Tests
# =============================================================================


class TestEnums:
    """Tests for enum types."""

    def test_conflict_modes(self):
        """Test ConflictMode enum values."""
        assert ConflictMode.SKIP.value == "skip"
        assert ConflictMode.OVERWRITE.value == "overwrite"
        assert ConflictMode.MERGE.value == "merge"
        assert ConflictMode.FAIL.value == "fail"

    def test_content_modes(self):
        """Test ContentMode enum values."""
        assert ContentMode.INCLUDE.value == "include"
        assert ContentMode.REFERENCE.value == "reference"
        assert ContentMode.SKIP.value == "skip"

    def test_enum_string_conversion(self):
        """Test enum to string conversion."""
        assert str(ConflictMode.SKIP) == "skip"
        assert str(ContentMode.INCLUDE) == "include"
