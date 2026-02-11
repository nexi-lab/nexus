"""Unit tests for ReBAC permission export/import in the portability module.

Tests:
- Export produces correct JSONL from ReBAC tuples
- Import writes tuples to ReBAC with correct remapping
- Round-trip preserves the permission graph
- Graph validation catches invalid records
- ID remapping works correctly

References:
- Issue #1255: ReBAC permission export/import in portability module
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.portability import (
    ZoneImportOptions,
    ZoneImportService,
    export_zone_bundle,
    import_zone_bundle,
)
from nexus.portability.models import PermissionRecord
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_nexus_fs(data_dir: Path):
    """Create a NexusFS with permissions and ReBAC enabled."""
    data_dir.mkdir(parents=True, exist_ok=True)
    return create_nexus_fs(
        backend=LocalBackend(data_dir),
        metadata_store=RaftMetadataStore.embedded(str(data_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=data_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=True,
    )


@pytest.fixture
def source_fs(temp_dir):
    """Source NexusFS with test files and permissions."""
    fs = _make_nexus_fs(temp_dir / "source")
    admin = OperationContext(user="admin", groups=[], is_admin=True)

    # Create test files
    fs.write("/workspace/readme.md", b"# Project", context=admin)
    fs.write("/workspace/src/main.py", b"print('hi')", context=admin)

    # Grant permissions via ReBAC
    rebac = fs._rebac_manager
    if rebac is not None:
        rebac.rebac_write(
            subject=("user", "alice"),
            relation="reader",
            object=("file", "/workspace/readme.md"),
            zone_id="test-zone",
        )
        rebac.rebac_write(
            subject=("user", "bob"),
            relation="writer",
            object=("file", "/workspace/src/main.py"),
            zone_id="test-zone",
        )
        rebac.rebac_write(
            subject=("group", "team-alpha"),
            relation="reader",
            object=("directory", "/workspace"),
            zone_id="test-zone",
        )

    yield fs
    fs.close()


@pytest.fixture
def target_fs(temp_dir):
    """Target NexusFS for import."""
    fs = _make_nexus_fs(temp_dir / "target")
    yield fs
    fs.close()


class TestPermissionExport:
    """Tests for _export_permissions in ZoneExportService."""

    def test_export_produces_permission_records(self, source_fs, temp_dir):
        """Export should write ReBAC tuples as JSONL."""
        bundle_path = temp_dir / "export.nexus"

        manifest = export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="test-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=True,
        )

        assert bundle_path.exists()
        assert manifest.permission_count == 3

    def test_export_without_permissions_flag(self, source_fs, temp_dir):
        """Export with include_permissions=False should skip permissions."""
        bundle_path = temp_dir / "no_perms.nexus"

        manifest = export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="test-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=False,
        )

        assert manifest.permission_count == 0

    def test_export_empty_zone_produces_zero_permissions(self, source_fs, temp_dir):
        """Export for a zone with no tuples should produce zero permissions."""
        bundle_path = temp_dir / "empty_zone.nexus"

        manifest = export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="nonexistent-zone",
            output_path=bundle_path,
            include_content=False,
            include_permissions=True,
        )

        assert manifest.permission_count == 0


class TestPermissionImport:
    """Tests for _import_permissions in ZoneImportService."""

    def test_import_writes_permissions_to_rebac(self, source_fs, target_fs, temp_dir):
        """Import should write permission tuples to the target ReBAC manager."""
        bundle_path = temp_dir / "roundtrip.nexus"

        export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="test-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=True,
        )

        result = import_zone_bundle(
            nexus_fs=target_fs,
            bundle_path=bundle_path,
            target_zone_id="target-zone",
            import_permissions=True,
        )

        assert result.success is True
        assert result.permissions_imported == 3
        assert result.permissions_skipped == 0

    def test_import_applies_user_remapping(self, source_fs, target_fs, temp_dir):
        """Import should remap user IDs according to user_id_remap."""
        bundle_path = temp_dir / "remap.nexus"

        export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="test-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=True,
        )

        options = ZoneImportOptions(
            bundle_path=bundle_path,
            target_zone_id="target-zone",
            import_permissions=True,
            user_id_remap={"alice": "alice-new", "bob": "bob-new"},
        )

        service = ZoneImportService(target_fs)
        result = service.import_zone(options)

        assert result.success is True
        assert result.permissions_imported == 3
        # alice and bob get remapped (2 user tuples), team-alpha group does not
        assert result.users_remapped == 2

    def test_import_applies_path_remapping(self, source_fs, target_fs, temp_dir):
        """Import should remap path prefixes in object_id."""
        bundle_path = temp_dir / "path_remap.nexus"

        export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="test-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=True,
        )

        options = ZoneImportOptions(
            bundle_path=bundle_path,
            target_zone_id="target-zone",
            import_permissions=True,
            path_prefix_remap={"/workspace": "/projects/migrated"},
        )

        service = ZoneImportService(target_fs)
        result = service.import_zone(options)

        assert result.success is True
        assert result.permissions_imported == 3

    def test_import_dry_run_does_not_write(self, source_fs, target_fs, temp_dir):
        """Dry run should count permissions without writing."""
        bundle_path = temp_dir / "dry.nexus"

        export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="test-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=True,
        )

        options = ZoneImportOptions(
            bundle_path=bundle_path,
            target_zone_id="target-zone",
            import_permissions=True,
            dry_run=True,
        )

        service = ZoneImportService(target_fs)
        result = service.import_zone(options)

        assert result.success is True
        assert result.permissions_imported == 3

        # Verify nothing actually written to target ReBAC
        target_rebac = target_fs._rebac_manager
        if target_rebac is not None:
            tuples = target_rebac._fetch_zone_tuples_from_db("target-zone")
            assert len(tuples) == 0


class TestPermissionRoundTrip:
    """Tests for full export -> import round trip."""

    def test_roundtrip_preserves_permission_graph(self, source_fs, target_fs, temp_dir):
        """Round-trip should preserve all permission tuples."""
        bundle_path = temp_dir / "roundtrip.nexus"

        # Export
        export_manifest = export_zone_bundle(
            nexus_fs=source_fs,
            zone_id="test-zone",
            output_path=bundle_path,
            include_content=True,
            include_permissions=True,
        )

        # Import
        import_result = import_zone_bundle(
            nexus_fs=target_fs,
            bundle_path=bundle_path,
            target_zone_id="target-zone",
            import_permissions=True,
        )

        assert import_result.success is True
        assert import_result.permissions_imported == export_manifest.permission_count

        # Verify tuples exist in target
        target_rebac = target_fs._rebac_manager
        if target_rebac is not None:
            target_tuples = target_rebac._fetch_zone_tuples_from_db("target-zone")
            assert len(target_tuples) == 3

            # Verify specific tuples
            subjects = {(t["subject_type"], t["subject_id"]) for t in target_tuples}
            assert ("user", "alice") in subjects
            assert ("user", "bob") in subjects
            assert ("group", "team-alpha") in subjects


class TestPermissionGraphValidation:
    """Tests for validate_permission_graph."""

    def test_valid_records_pass(self):
        """Valid permission records should produce no errors."""
        records = [
            PermissionRecord(
                object_type="file",
                object_id="/workspace/readme.md",
                relation="reader",
                subject_type="user",
                subject_id="alice",
            ),
            PermissionRecord(
                object_type="directory",
                object_id="/workspace",
                relation="writer",
                subject_type="group",
                subject_id="team-alpha",
            ),
        ]

        errors = ZoneImportService.validate_permission_graph(records)
        assert errors == []

    def test_missing_subject_type(self):
        """Missing subject_type should produce an error."""
        records = [
            PermissionRecord(
                object_type="file",
                object_id="/test",
                relation="reader",
                subject_type="",
                subject_id="alice",
            ),
        ]

        errors = ZoneImportService.validate_permission_graph(records)
        assert len(errors) == 1
        assert "missing subject_type" in errors[0]

    def test_missing_relation(self):
        """Missing relation should produce an error."""
        records = [
            PermissionRecord(
                object_type="file",
                object_id="/test",
                relation="",
                subject_type="user",
                subject_id="alice",
            ),
        ]

        errors = ZoneImportService.validate_permission_graph(records)
        assert len(errors) == 1
        assert "missing relation" in errors[0]

    def test_unknown_subject_type(self):
        """Unknown subject_type should produce an error."""
        records = [
            PermissionRecord(
                object_type="file",
                object_id="/test",
                relation="reader",
                subject_type="robot",
                subject_id="r2d2",
            ),
        ]

        errors = ZoneImportService.validate_permission_graph(records)
        assert len(errors) == 1
        assert "unknown subject_type" in errors[0]

    def test_self_referential_tuple(self):
        """Self-referential tuple should produce an error."""
        records = [
            PermissionRecord(
                object_type="user",
                object_id="alice",
                relation="reader",
                subject_type="user",
                subject_id="alice",
            ),
        ]

        errors = ZoneImportService.validate_permission_graph(records)
        assert len(errors) == 1
        assert "self-referential" in errors[0]

    def test_duplicate_tuples(self):
        """Duplicate tuples should produce an error."""
        rec = PermissionRecord(
            object_type="file",
            object_id="/test",
            relation="reader",
            subject_type="user",
            subject_id="alice",
        )

        errors = ZoneImportService.validate_permission_graph([rec, rec])
        assert len(errors) == 1
        assert "duplicate" in errors[0]

    def test_empty_list_is_valid(self):
        """Empty record list should produce no errors."""
        errors = ZoneImportService.validate_permission_graph([])
        assert errors == []
