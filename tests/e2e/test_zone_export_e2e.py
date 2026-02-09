"""End-to-end tests for zone export functionality.

Tests the complete export workflow including:
- Creating test files
- Exporting to .nexus bundle
- Validating bundle integrity
- Reading bundle contents
"""

from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.factory import create_nexus_fs
from nexus.portability import (
    BundleReader,
    ZoneExportOptions,
    ZoneExportService,
    export_zone_bundle,
    inspect_bundle,
    validate_bundle,
)
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nexus_fs(temp_dir):
    """Create NexusFS instance with test data."""
    data_dir = temp_dir / "data"
    data_dir.mkdir()

    fs = create_nexus_fs(
        backend=LocalBackend(data_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=data_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=data_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=False,
    )

    # Create test files
    fs.write("/workspace/readme.md", b"# Test Project\n\nThis is a test.")
    fs.write("/workspace/src/main.py", b'print("Hello, World!")')
    fs.write("/workspace/src/utils.py", b"def helper(): pass")
    fs.write("/docs/guide.txt", b"User guide content here.")

    yield fs
    fs.close()


class TestZoneExportService:
    """Tests for ZoneExportService."""

    def test_export_creates_bundle(self, nexus_fs, temp_dir):
        """Test that export creates a valid .nexus bundle."""
        output_path = temp_dir / "export.nexus"

        options = ZoneExportOptions(
            output_path=output_path,
            include_content=True,
            include_permissions=True,
        )

        service = ZoneExportService(nexus_fs)
        manifest = service.export_zone("default", options)

        # Verify bundle was created
        assert output_path.exists()
        assert output_path.stat().st_size > 0

        # Verify manifest has correct stats
        assert manifest.file_count == 4
        assert manifest.total_size_bytes > 0
        assert manifest.content_blob_count > 0
        assert manifest.source_zone_id == "default"

    def test_export_with_path_prefix_filter(self, nexus_fs, temp_dir):
        """Test export with path prefix filtering."""
        output_path = temp_dir / "workspace_only.nexus"

        options = ZoneExportOptions(
            output_path=output_path,
            include_content=True,
            path_prefix="/workspace",
        )

        service = ZoneExportService(nexus_fs)
        manifest = service.export_zone("default", options)

        # Should only export workspace files (3 files)
        assert manifest.file_count == 3
        assert manifest.path_prefix_filter == "/workspace"

    def test_export_without_content(self, nexus_fs, temp_dir):
        """Test metadata-only export (no content blobs)."""
        output_path = temp_dir / "metadata_only.nexus"

        options = ZoneExportOptions(
            output_path=output_path,
            include_content=False,
            include_permissions=False,
        )

        service = ZoneExportService(nexus_fs)
        manifest = service.export_zone("default", options)

        # Verify no content was exported
        assert manifest.file_count == 4
        assert manifest.content_blob_count == 0
        assert manifest.include_content is False

        # Bundle should be smaller without content
        assert output_path.stat().st_size < 5000  # Small metadata-only bundle


class TestBundleReader:
    """Tests for BundleReader."""

    def test_read_manifest(self, nexus_fs, temp_dir):
        """Test reading manifest from bundle."""
        output_path = temp_dir / "test.nexus"

        # Create bundle
        manifest = export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
        )

        # Read bundle
        with BundleReader(output_path) as reader:
            read_manifest = reader.get_manifest()

            assert read_manifest.bundle_id == manifest.bundle_id
            assert read_manifest.file_count == manifest.file_count
            assert read_manifest.source_zone_id == "default"

    def test_iter_file_records(self, nexus_fs, temp_dir):
        """Test iterating over file records."""
        output_path = temp_dir / "test.nexus"

        export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
        )

        with BundleReader(output_path) as reader:
            records = list(reader.iter_file_records())

            assert len(records) == 4

            paths = {r.virtual_path for r in records}
            assert "/workspace/readme.md" in paths
            assert "/workspace/src/main.py" in paths
            assert "/docs/guide.txt" in paths

    def test_read_content_blob(self, nexus_fs, temp_dir):
        """Test reading content blobs from bundle."""
        output_path = temp_dir / "test.nexus"

        export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
        )

        with BundleReader(output_path) as reader:
            # Get a file record to find its content hash
            records = list(reader.iter_file_records())
            readme_record = next(r for r in records if "readme" in r.virtual_path)

            if readme_record.content_hash:
                content = reader.read_content_blob(readme_record.content_hash)
                assert content is not None
                assert b"Test Project" in content

    def test_list_contents(self, nexus_fs, temp_dir):
        """Test listing bundle contents."""
        output_path = temp_dir / "test.nexus"

        export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
        )

        with BundleReader(output_path) as reader:
            contents = reader.list_contents()

            assert "manifest.json" in contents
            assert "metadata/files.jsonl" in contents


class TestValidateBundle:
    """Tests for bundle validation."""

    def test_validate_valid_bundle(self, nexus_fs, temp_dir):
        """Test validation of valid bundle."""
        output_path = temp_dir / "valid.nexus"

        export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
        )

        is_valid, errors = validate_bundle(output_path)

        assert is_valid is True
        assert len(errors) == 0

    def test_validate_missing_manifest(self, temp_dir):
        """Test validation fails for bundle without manifest."""
        # Create a tar.gz without manifest
        invalid_bundle = temp_dir / "invalid.nexus"
        with tarfile.open(invalid_bundle, "w:gz") as tar:
            # Add a dummy file
            dummy_path = temp_dir / "dummy.txt"
            dummy_path.write_text("dummy")
            tar.add(dummy_path, arcname="dummy.txt")

        is_valid, errors = validate_bundle(invalid_bundle)

        assert is_valid is False
        assert any("manifest" in e.lower() for e in errors)

    def test_validate_nonexistent_bundle(self, temp_dir):
        """Test validation fails for nonexistent bundle."""
        is_valid, errors = validate_bundle(temp_dir / "nonexistent.nexus")

        assert is_valid is False
        assert len(errors) > 0


class TestInspectBundle:
    """Tests for bundle inspection."""

    def test_inspect_bundle(self, nexus_fs, temp_dir):
        """Test bundle inspection returns correct info."""
        output_path = temp_dir / "test.nexus"

        export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
        )

        info = inspect_bundle(output_path)

        assert info["file_count"] == 4
        assert info["source_zone_id"] == "default"
        assert info["include_content"] is True
        assert "bundle_id" in info
        assert "export_timestamp" in info


class TestExportConvenienceFunction:
    """Tests for export_zone_bundle convenience function."""

    def test_export_zone_bundle(self, nexus_fs, temp_dir):
        """Test convenience function creates valid bundle."""
        output_path = temp_dir / "convenience.nexus"

        manifest = export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
            include_content=True,
            include_permissions=True,
            path_prefix="/workspace",
        )

        assert output_path.exists()
        assert manifest.file_count == 3  # Only workspace files
        assert manifest.include_content is True

    def test_export_with_progress_callback(self, nexus_fs, temp_dir):
        """Test export with progress callback."""
        output_path = temp_dir / "progress.nexus"
        progress_calls = []

        def on_progress(current: int, total: int) -> None:
            progress_calls.append((current, total))

        export_zone_bundle(
            nexus_fs=nexus_fs,
            zone_id="default",
            output_path=output_path,
            progress_callback=on_progress,
        )

        # Should have received progress updates
        assert len(progress_calls) > 0
        # Last call should show completion
        last_call = progress_calls[-1]
        assert last_call[0] == last_call[1]  # current == total
