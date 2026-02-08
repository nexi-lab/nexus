"""End-to-end tests for zone portability with permissions enabled.

Tests the export/import workflow with enforce_permissions=True to ensure
the portability module works correctly with the permission system.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext
from nexus.portability import (
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore
    ConflictMode,
    ZoneImportOptions,
    ZoneImportService,
    export_zone_bundle,
    import_zone_bundle,
)


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def source_nexus_fs_with_permissions(temp_dir):
    """Create source NexusFS with permissions enabled."""
    data_dir = temp_dir / "source_data"
    data_dir.mkdir()

    fs = NexusFS(
        backend=LocalBackend(data_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=data_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=data_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=True,  # Enable permissions
    )

    # Create admin context for writing test files
    admin_context = OperationContext(user="admin", groups=[], is_admin=True)

    # Create test files as admin
    fs.write("/workspace/readme.md", b"# Test Project\n\nPermissions test.", context=admin_context)
    fs.write("/workspace/src/main.py", b'print("Hello with permissions!")', context=admin_context)
    fs.write("/docs/guide.txt", b"User guide with permissions.", context=admin_context)

    yield fs
    fs.close()


@pytest.fixture
def target_nexus_fs_with_permissions(temp_dir):
    """Create target NexusFS with permissions enabled."""
    data_dir = temp_dir / "target_data"
    data_dir.mkdir()

    fs = NexusFS(
        backend=LocalBackend(data_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=data_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=data_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=True,  # Enable permissions
    )

    yield fs
    fs.close()


@pytest.fixture
def exported_bundle_with_permissions(source_nexus_fs_with_permissions, temp_dir):
    """Create an exported bundle from permission-enabled source."""
    output_path = temp_dir / "export_perms.nexus"

    export_zone_bundle(
        nexus_fs=source_nexus_fs_with_permissions,
        zone_id="source-zone",
        output_path=output_path,
        include_content=True,
        include_permissions=True,
    )

    return output_path


class TestExportWithPermissions:
    """Tests for export with permissions enabled."""

    def test_export_creates_bundle_with_permissions(
        self, source_nexus_fs_with_permissions, temp_dir
    ):
        """Test that export works with permissions enabled."""
        output_path = temp_dir / "perms_export.nexus"

        manifest = export_zone_bundle(
            nexus_fs=source_nexus_fs_with_permissions,
            zone_id="test-zone",
            output_path=output_path,
            include_content=True,
            include_permissions=True,
        )

        # Verify bundle was created
        assert output_path.exists()
        assert manifest.file_count == 3
        assert manifest.content_blob_count > 0

    def test_export_respects_path_filter_with_permissions(
        self, source_nexus_fs_with_permissions, temp_dir
    ):
        """Test path filtering works with permissions enabled."""
        output_path = temp_dir / "filtered_perms.nexus"

        manifest = export_zone_bundle(
            nexus_fs=source_nexus_fs_with_permissions,
            zone_id="test-zone",
            output_path=output_path,
            path_prefix="/workspace",
        )

        # Should only export workspace files
        assert manifest.file_count == 2


class TestImportWithPermissions:
    """Tests for import with permissions enabled."""

    def test_import_with_permissions_enabled(
        self, exported_bundle_with_permissions, target_nexus_fs_with_permissions
    ):
        """Test that import works with permissions enabled on target."""
        result = import_zone_bundle(
            nexus_fs=target_nexus_fs_with_permissions,
            bundle_path=exported_bundle_with_permissions,
        )

        assert result.success is True
        assert result.files_created == 3

        # Verify files exist via metadata store (bypasses permission checks)
        admin_context = OperationContext(user="admin", groups=[], is_admin=True)

        # Check via metadata directly
        meta = target_nexus_fs_with_permissions.metadata.get("/workspace/readme.md")
        assert meta is not None, "File metadata not found for /workspace/readme.md"

        # Read content as admin
        content = target_nexus_fs_with_permissions.read(
            "/workspace/readme.md", context=admin_context
        )
        assert b"Permissions test" in content

    def test_import_conflict_skip_with_permissions(
        self, exported_bundle_with_permissions, target_nexus_fs_with_permissions
    ):
        """Test SKIP conflict mode with permissions enabled."""
        admin_context = OperationContext(user="admin", groups=[], is_admin=True)

        # Create existing file
        target_nexus_fs_with_permissions.write(
            "/workspace/readme.md", b"Existing content", context=admin_context
        )

        options = ZoneImportOptions(
            bundle_path=exported_bundle_with_permissions,
            conflict_mode=ConflictMode.SKIP,
        )

        service = ZoneImportService(target_nexus_fs_with_permissions)
        result = service.import_zone(options)

        assert result.success is True
        assert result.files_skipped == 1
        assert result.files_created == 2

        # Original content preserved
        content = target_nexus_fs_with_permissions.read(
            "/workspace/readme.md", context=admin_context
        )
        assert content == b"Existing content"

    def test_import_overwrite_with_permissions(
        self, exported_bundle_with_permissions, target_nexus_fs_with_permissions
    ):
        """Test OVERWRITE conflict mode with permissions enabled."""
        admin_context = OperationContext(user="admin", groups=[], is_admin=True)

        # Create existing file
        target_nexus_fs_with_permissions.write(
            "/workspace/readme.md", b"Existing content", context=admin_context
        )

        options = ZoneImportOptions(
            bundle_path=exported_bundle_with_permissions,
            conflict_mode=ConflictMode.OVERWRITE,
        )

        service = ZoneImportService(target_nexus_fs_with_permissions)
        result = service.import_zone(options)

        assert result.success is True
        assert result.files_updated == 1
        assert result.files_created == 2

        # Content should be from bundle
        content = target_nexus_fs_with_permissions.read(
            "/workspace/readme.md", context=admin_context
        )
        assert b"Permissions test" in content


class TestRoundTripWithPermissions:
    """Tests for export -> import round trip with permissions."""

    def test_roundtrip_preserves_content_with_permissions(
        self, source_nexus_fs_with_permissions, target_nexus_fs_with_permissions, temp_dir
    ):
        """Test that content is preserved through export/import with permissions."""
        bundle_path = temp_dir / "roundtrip_perms.nexus"
        admin_context = OperationContext(user="admin", groups=[], is_admin=True)

        # Export from source
        export_manifest = export_zone_bundle(
            nexus_fs=source_nexus_fs_with_permissions,
            zone_id="source",
            output_path=bundle_path,
            include_content=True,
        )

        # Import to target
        import_result = import_zone_bundle(
            nexus_fs=target_nexus_fs_with_permissions,
            bundle_path=bundle_path,
        )

        # Verify round trip
        assert import_result.success is True
        assert import_result.files_created == export_manifest.file_count

        # Verify content matches
        for path in ["/workspace/readme.md", "/workspace/src/main.py", "/docs/guide.txt"]:
            source_content = source_nexus_fs_with_permissions.read(path, context=admin_context)
            target_content = target_nexus_fs_with_permissions.read(path, context=admin_context)
            assert source_content == target_content, f"Content mismatch for {path}"
