"""End-to-end tests for zone import functionality.

Tests the complete import workflow including:
- Importing from .nexus bundles
- Conflict resolution modes
- Path remapping
- Dry run mode
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.factory import create_nexus_fs
from nexus.portability import (
    ConflictMode,
    ZoneImportOptions,
    ZoneImportService,
    export_zone_bundle,
    import_zone_bundle,
)
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def source_nexus_fs(temp_dir):
    """Create source NexusFS instance with test data for export."""
    data_dir = temp_dir / "source_data"
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


@pytest.fixture
def target_nexus_fs(temp_dir):
    """Create target NexusFS instance for import."""
    data_dir = temp_dir / "target_data"
    data_dir.mkdir()

    fs = create_nexus_fs(
        backend=LocalBackend(data_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=data_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=data_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=False,
    )

    yield fs
    fs.close()


@pytest.fixture
def exported_bundle(source_nexus_fs, temp_dir):
    """Create an exported bundle for import tests."""
    output_path = temp_dir / "export.nexus"

    export_zone_bundle(
        nexus_fs=source_nexus_fs,
        zone_id="source-zone",
        output_path=output_path,
        include_content=True,
        include_permissions=True,
    )

    return output_path


class TestZoneImportService:
    """Tests for ZoneImportService."""

    def test_import_creates_files(self, exported_bundle, target_nexus_fs):
        """Test that import creates files from bundle."""
        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            conflict_mode=ConflictMode.SKIP,
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        # Verify import succeeded
        assert result.success is True
        assert result.files_created == 4
        assert result.files_failed == 0

        # Verify files exist
        assert target_nexus_fs.exists("/workspace/readme.md")
        assert target_nexus_fs.exists("/workspace/src/main.py")
        assert target_nexus_fs.exists("/docs/guide.txt")

        # Verify content is correct
        content = target_nexus_fs.read("/workspace/readme.md")
        assert b"Test Project" in content

    def test_import_with_target_zone_remap(self, exported_bundle, target_nexus_fs):
        """Test import with zone ID remapping."""
        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            target_zone_id="new-zone",
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        assert result.success is True
        assert result.zone_remapped is True
        assert result.files_created == 4

    def test_import_dry_run(self, exported_bundle, target_nexus_fs):
        """Test dry run mode doesn't create files."""
        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            dry_run=True,
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        # Dry run should report files that would be created
        assert result.files_created == 4

        # But files should not actually exist
        assert not target_nexus_fs.exists("/workspace/readme.md")
        assert not target_nexus_fs.exists("/docs/guide.txt")


class TestConflictResolution:
    """Tests for conflict resolution modes."""

    def test_conflict_skip(self, exported_bundle, target_nexus_fs):
        """Test SKIP mode keeps existing files."""
        # Create an existing file
        target_nexus_fs.write("/workspace/readme.md", b"Existing content")

        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            conflict_mode=ConflictMode.SKIP,
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        assert result.success is True
        assert result.files_skipped == 1
        assert result.files_created == 3  # Other 3 files created

        # Original content should be preserved
        content = target_nexus_fs.read("/workspace/readme.md")
        assert content == b"Existing content"

    def test_conflict_overwrite(self, exported_bundle, target_nexus_fs):
        """Test OVERWRITE mode replaces existing files."""
        # Create an existing file
        target_nexus_fs.write("/workspace/readme.md", b"Existing content")

        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            conflict_mode=ConflictMode.OVERWRITE,
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        assert result.success is True
        assert result.files_updated == 1
        assert result.files_created == 3

        # Content should be from bundle
        content = target_nexus_fs.read("/workspace/readme.md")
        assert b"Test Project" in content

    def test_conflict_fail(self, exported_bundle, target_nexus_fs):
        """Test FAIL mode stops on first conflict."""
        # Create an existing file
        target_nexus_fs.write("/workspace/readme.md", b"Existing content")

        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            conflict_mode=ConflictMode.FAIL,
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        # Import should fail
        assert result.success is False
        assert len(result.errors) > 0


class TestPathRemapping:
    """Tests for path prefix remapping."""

    def test_path_remap(self, exported_bundle, target_nexus_fs):
        """Test path prefix remapping during import."""
        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            path_prefix_remap={"/workspace/": "/projects/"},
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        assert result.success is True
        assert result.paths_remapped >= 3  # workspace files remapped

        # Files should be at new paths
        assert target_nexus_fs.exists("/projects/readme.md")
        assert target_nexus_fs.exists("/projects/src/main.py")

        # Original paths should not exist
        assert not target_nexus_fs.exists("/workspace/readme.md")

    def test_multiple_path_remaps(self, exported_bundle, target_nexus_fs):
        """Test multiple path prefix remappings."""
        options = ZoneImportOptions(
            bundle_path=exported_bundle,
            path_prefix_remap={
                "/workspace/": "/projects/",
                "/docs/": "/documentation/",
            },
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        assert result.success is True

        # Both remappings applied
        assert target_nexus_fs.exists("/projects/readme.md")
        assert target_nexus_fs.exists("/documentation/guide.txt")


class TestImportConvenienceFunction:
    """Tests for import_zone_bundle convenience function."""

    def test_import_zone_bundle(self, exported_bundle, target_nexus_fs):
        """Test convenience function imports bundle."""
        result = import_zone_bundle(
            nexus_fs=target_nexus_fs,
            bundle_path=exported_bundle,
        )

        assert result.success is True
        assert result.files_created == 4
        assert target_nexus_fs.exists("/workspace/readme.md")

    def test_import_with_progress_callback(self, exported_bundle, target_nexus_fs):
        """Test import with progress callback."""
        progress_calls = []

        def on_progress(current: int, total: int, phase: str) -> None:
            progress_calls.append((current, total, phase))

        result = import_zone_bundle(
            nexus_fs=target_nexus_fs,
            bundle_path=exported_bundle,
            progress_callback=on_progress,
        )

        assert result.success is True
        # Should have received progress updates
        assert len(progress_calls) > 0


class TestImportValidation:
    """Tests for import validation."""

    def test_import_nonexistent_bundle(self, target_nexus_fs, temp_dir):
        """Test import fails gracefully for nonexistent bundle."""
        options = ZoneImportOptions(
            bundle_path=temp_dir / "nonexistent.nexus",
        )

        service = ZoneImportService(target_nexus_fs)
        result = service.import_zone(options)

        assert result.success is False
        assert any(e.error_type == "file_not_found" for e in result.errors)

    def test_import_statistics(self, exported_bundle, target_nexus_fs):
        """Test import result statistics are accurate."""
        result = import_zone_bundle(
            nexus_fs=target_nexus_fs,
            bundle_path=exported_bundle,
        )

        # Verify statistics
        assert result.total_files_processed == 4
        assert result.duration_seconds >= 0
        assert result.started_at is not None
        assert result.completed_at is not None


class TestRoundTrip:
    """Tests for export -> import round trip."""

    def test_export_import_roundtrip(self, source_nexus_fs, target_nexus_fs, temp_dir):
        """Test that exported data can be fully restored."""
        bundle_path = temp_dir / "roundtrip.nexus"

        # Export from source
        export_manifest = export_zone_bundle(
            nexus_fs=source_nexus_fs,
            zone_id="source",
            output_path=bundle_path,
            include_content=True,
        )

        # Import to target
        import_result = import_zone_bundle(
            nexus_fs=target_nexus_fs,
            bundle_path=bundle_path,
        )

        # Verify round trip
        assert import_result.success is True
        assert import_result.files_created == export_manifest.file_count

        # Verify all content matches
        for path in [
            "/workspace/readme.md",
            "/workspace/src/main.py",
            "/workspace/src/utils.py",
            "/docs/guide.txt",
        ]:
            source_content = source_nexus_fs.read(path)
            target_content = target_nexus_fs.read(path)
            assert source_content == target_content, f"Content mismatch for {path}"
