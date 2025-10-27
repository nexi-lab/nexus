"""Tests for metadata export/import functionality with advanced options."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from nexus.backends.local import LocalBackend
from nexus.core.export_import import CollisionDetail, ExportFilter, ImportOptions, ImportResult
from nexus.core.nexus_fs import NexusFS


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir):
    """Create Nexus filesystem instance."""
    fs = NexusFS(
        backend=LocalBackend(temp_dir / "data"),
        db_path=temp_dir / "data" / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,
    )
    yield fs
    fs.close()


def test_export_filter_defaults():
    """Test ExportFilter default values."""
    filter = ExportFilter()
    assert filter.tenant_id is None
    assert filter.path_prefix == ""
    assert filter.after_time is None
    assert filter.include_deleted is False


def test_export_filter_custom_values():
    """Test ExportFilter with custom values."""
    after_time = datetime(2024, 1, 1, tzinfo=UTC)
    filter = ExportFilter(
        tenant_id="test-tenant",
        path_prefix="/workspace",
        after_time=after_time,
        include_deleted=True,
    )
    assert filter.tenant_id == "test-tenant"
    assert filter.path_prefix == "/workspace"
    assert filter.after_time == after_time
    assert filter.include_deleted is True


def test_import_options_defaults():
    """Test ImportOptions default values."""
    options = ImportOptions()
    assert options.dry_run is False
    assert options.conflict_mode == "skip"
    assert options.preserve_ids is True


def test_import_options_custom_values():
    """Test ImportOptions with custom values."""
    options = ImportOptions(
        dry_run=True,
        conflict_mode="overwrite",
        preserve_ids=False,
    )
    assert options.dry_run is True
    assert options.conflict_mode == "overwrite"
    assert options.preserve_ids is False


def test_import_result_defaults():
    """Test ImportResult default values."""
    result = ImportResult()
    assert result.created == 0
    assert result.updated == 0
    assert result.skipped == 0
    assert result.remapped == 0
    assert result.collisions == []
    assert result.total_processed == 0


def test_import_result_total_processed():
    """Test ImportResult.total_processed property."""
    result = ImportResult(created=5, updated=3, skipped=2, remapped=1)
    assert result.total_processed == 11


def test_import_result_str():
    """Test ImportResult string representation."""
    result = ImportResult(created=5, updated=3, skipped=2, remapped=1)
    result.collisions.append(
        CollisionDetail(
            path="/test",
            existing_etag="abc",
            imported_etag="def",
            resolution="skip",
            message="test",
        )
    )
    s = str(result)
    assert "created=5" in s
    assert "updated=3" in s
    assert "skipped=2" in s
    assert "remapped=1" in s
    assert "collisions=1" in s


def test_collision_detail():
    """Test CollisionDetail dataclass."""
    detail = CollisionDetail(
        path="/test/file.txt",
        existing_etag="abc123",
        imported_etag="def456",
        resolution="skip",
        message="Skipped: existing file has different content",
    )
    assert detail.path == "/test/file.txt"
    assert detail.existing_etag == "abc123"
    assert detail.imported_etag == "def456"
    assert detail.resolution == "skip"
    assert "Skipped" in detail.message


def test_export_basic(nx, temp_dir):
    """Test basic export functionality."""
    # Create test files
    nx.write("/file1.txt", b"content1")
    nx.write("/file2.txt", b"content2")
    nx.write("/workspace/file3.txt", b"content3")

    # Export all metadata
    output_path = temp_dir / "export.jsonl"
    count = nx.export_metadata(output_path)

    assert count == 3
    assert output_path.exists()

    # Verify JSONL format
    lines = output_path.read_text().strip().split("\n")
    assert len(lines) == 3

    # Parse and verify each line
    for line in lines:
        data = json.loads(line)
        assert "path" in data
        assert "backend_name" in data
        assert "size" in data
        assert "etag" in data


def test_export_with_prefix(nx, temp_dir):
    """Test export with path prefix filter."""
    # Create test files
    nx.write("/file1.txt", b"content1")
    nx.write("/workspace/file2.txt", b"content2")
    nx.write("/workspace/data/file3.txt", b"content3")

    # Export with prefix
    output_path = temp_dir / "export.jsonl"
    filter = ExportFilter(path_prefix="/workspace")
    count = nx.export_metadata(output_path, filter=filter)

    assert count == 2

    # Verify only workspace files exported
    lines = output_path.read_text().strip().split("\n")
    for line in lines:
        data = json.loads(line)
        assert data["path"].startswith("/workspace")


def test_export_with_time_filter(nx, temp_dir):
    """Test export with after_time filter."""
    with freeze_time("2025-01-01 12:00:00") as frozen_time:
        # Create old file
        nx.write("/old_file.txt", b"old content")

        # Advance time by 1 minute
        frozen_time.tick(delta=timedelta(minutes=1))
        cutoff_time = datetime.now(UTC)

        # Advance time by another minute
        frozen_time.tick(delta=timedelta(minutes=1))
        nx.write("/new_file.txt", b"new content")

        # Export only files after cutoff
        output_path = temp_dir / "export.jsonl"
        filter = ExportFilter(after_time=cutoff_time)
        count = nx.export_metadata(output_path, filter=filter)

        # Should only export new file
        assert count == 1

        lines = output_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
    assert data["path"] == "/new_file.txt"


def test_export_sorted_output(nx, temp_dir):
    """Test that export output is sorted by path."""
    # Create files in random order
    nx.write("/z_last.txt", b"last")
    nx.write("/a_first.txt", b"first")
    nx.write("/m_middle.txt", b"middle")

    # Export
    output_path = temp_dir / "export.jsonl"
    nx.export_metadata(output_path)

    # Verify sorted order
    lines = output_path.read_text().strip().split("\n")
    paths = [json.loads(line)["path"] for line in lines]

    assert paths == ["/a_first.txt", "/m_middle.txt", "/z_last.txt"]


def test_import_basic(nx, temp_dir):
    """Test basic import functionality."""
    # Create and export files
    nx.write("/file1.txt", b"content1")
    nx.write("/file2.txt", b"content2")

    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Delete files
    nx.delete("/file1.txt")
    nx.delete("/file2.txt")

    # Import
    result = nx.import_metadata(export_path)

    assert result.created == 2
    assert result.updated == 0
    assert result.skipped == 0
    assert result.total_processed == 2
    assert len(result.collisions) == 0

    # Verify files restored
    assert nx.exists("/file1.txt")
    assert nx.exists("/file2.txt")


def test_import_conflict_mode_skip(nx, temp_dir):
    """Test import with skip conflict mode."""
    # Create file
    nx.write("/file.txt", b"original content")
    original_meta = nx.metadata.get("/file.txt")

    # Export
    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Modify file
    nx.write("/file.txt", b"modified content")

    # Import with skip mode (default)
    options = ImportOptions(conflict_mode="skip")
    result = nx.import_metadata(export_path, options=options)

    assert result.created == 0
    assert result.updated == 0
    assert result.skipped == 1
    assert len(result.collisions) == 1

    collision = result.collisions[0]
    assert collision.path == "/file.txt"
    assert collision.resolution == "skip"

    # Verify file not changed
    current_meta = nx.metadata.get("/file.txt")
    assert current_meta.etag != original_meta.etag


def test_import_conflict_mode_overwrite(nx, temp_dir):
    """Test import with overwrite conflict mode."""
    # Create file
    nx.write("/file.txt", b"original content")
    original_meta = nx.metadata.get("/file.txt")

    # Export
    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Modify file
    nx.write("/file.txt", b"modified content")

    # Import with overwrite mode
    options = ImportOptions(conflict_mode="overwrite")
    result = nx.import_metadata(export_path, options=options)

    assert result.created == 0
    assert result.updated == 1
    assert result.skipped == 0
    assert len(result.collisions) == 1

    collision = result.collisions[0]
    assert collision.path == "/file.txt"
    assert collision.resolution == "overwrite"

    # Verify file restored to original
    current_meta = nx.metadata.get("/file.txt")
    assert current_meta.etag == original_meta.etag


def test_import_conflict_mode_remap(nx, temp_dir):
    """Test import with remap conflict mode."""
    # Create file
    nx.write("/file.txt", b"original content")

    # Export
    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Modify file (create collision)
    nx.write("/file.txt", b"modified content")

    # Import with remap mode
    options = ImportOptions(conflict_mode="remap")
    result = nx.import_metadata(export_path, options=options)

    assert result.created == 0
    assert result.updated == 0
    assert result.remapped == 1
    assert len(result.collisions) == 1

    collision = result.collisions[0]
    assert collision.path == "/file.txt"
    assert collision.resolution == "remap"
    assert "_imported" in collision.message

    # Verify both files exist
    assert nx.exists("/file.txt")
    assert nx.exists("/file.txt_imported1")


def test_import_conflict_mode_auto_newer_imported(nx, temp_dir):
    """Test import with auto mode - imported is newer."""
    with freeze_time("2025-01-01 12:00:00") as frozen_time:
        # Create old file
        nx.write("/file.txt", b"old content")

        # Advance time to ensure different timestamp
        frozen_time.tick(delta=timedelta(minutes=1))

        # Export (will have newer timestamp)
        export_path = temp_dir / "export.jsonl"

        # Manually create export with future timestamp
        export_data = {
            "path": "/file.txt",
            "backend_name": "local",
            "physical_path": "abc123",
            "size": 11,
            "etag": "abc123",
            "created_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "modified_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "version": 1,
        }

        with open(export_path, "w") as f:
            f.write(json.dumps(export_data) + "\n")

        # Import with auto mode
        options = ImportOptions(conflict_mode="auto")
        result = nx.import_metadata(export_path, options=options)

        # Should overwrite because imported is newer
        assert result.created == 0
        assert result.updated == 1
        assert result.skipped == 0
        assert len(result.collisions) == 1

        collision = result.collisions[0]
        assert "auto_overwrite" in collision.resolution


def test_import_conflict_mode_auto_existing_newer(nx, temp_dir):
    """Test import with auto mode - existing is newer."""
    with freeze_time("2025-01-01 12:00:00") as frozen_time:
        # Create file
        nx.write("/file.txt", b"content")

        # Export
        export_path = temp_dir / "export.jsonl"
        nx.export_metadata(export_path)

        # Advance time and update file (make it newer)
        frozen_time.tick(delta=timedelta(minutes=1))
        nx.write("/file.txt", b"newer content")

        # Import with auto mode
        options = ImportOptions(conflict_mode="auto")
        result = nx.import_metadata(export_path, options=options)

        # Should skip because existing is newer
        assert result.created == 0
        assert result.updated == 0
        assert result.skipped == 1
        assert len(result.collisions) == 1

    collision = result.collisions[0]
    assert "auto_skip" in collision.resolution


def test_import_dry_run(nx, temp_dir):
    """Test import with dry_run mode."""
    # Create file
    nx.write("/file.txt", b"original content")

    # Export
    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Delete file
    nx.delete("/file.txt")

    # Import with dry_run
    options = ImportOptions(dry_run=True)
    result = nx.import_metadata(export_path, options=options)

    # Should report as created but not actually import
    assert result.created == 1
    assert result.updated == 0
    assert result.skipped == 0

    # Verify file NOT restored
    assert not nx.exists("/file.txt")


def test_import_dry_run_with_conflicts(nx, temp_dir):
    """Test import dry_run with conflicts."""
    # Create file
    nx.write("/file.txt", b"original content")

    # Export
    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Modify file
    nx.write("/file.txt", b"modified content")
    modified_meta = nx.metadata.get("/file.txt")

    # Import with dry_run and overwrite mode
    options = ImportOptions(dry_run=True, conflict_mode="overwrite")
    result = nx.import_metadata(export_path, options=options)

    # Should report as updated but not actually import
    assert result.created == 0
    assert result.updated == 1
    assert result.skipped == 0
    assert len(result.collisions) == 1

    # Verify file NOT changed
    current_meta = nx.metadata.get("/file.txt")
    assert current_meta.etag == modified_meta.etag


def test_import_backward_compatibility_overwrite(nx, temp_dir):
    """Test import with old overwrite parameter."""
    # Create file
    nx.write("/file.txt", b"original content")
    original_meta = nx.metadata.get("/file.txt")

    # Export
    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Modify file
    nx.write("/file.txt", b"modified content")

    # Import with old API (overwrite=True)
    result = nx.import_metadata(export_path, overwrite=True)

    # Should behave like conflict_mode=overwrite
    assert result.created == 0
    assert result.updated == 1
    assert result.skipped == 0

    # Verify file restored
    current_meta = nx.metadata.get("/file.txt")
    assert current_meta.etag == original_meta.etag


def test_import_same_content_different_metadata(nx, temp_dir):
    """Test import when file has same content but different metadata."""
    # Create file
    nx.write("/file.txt", b"content")

    # Export
    export_path = temp_dir / "export.jsonl"
    nx.export_metadata(export_path)

    # Modify metadata in export (simulate different timestamps)
    with open(export_path) as f:
        data = json.loads(f.read().strip())

    # Keep same etag but change timestamp
    data["modified_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    with open(export_path, "w") as f:
        f.write(json.dumps(data) + "\n")

    # Import
    result = nx.import_metadata(export_path)

    # Should update metadata (same content, different metadata)
    assert result.created == 0
    assert result.updated == 1
    assert result.skipped == 0
    assert len(result.collisions) == 0


def test_import_invalid_jsonl_format(nx, temp_dir):
    """Test import with invalid JSONL format."""
    # Create invalid JSONL file
    export_path = temp_dir / "invalid.jsonl"
    export_path.write_text("{ invalid json }\n")

    # Should raise ValueError
    with pytest.raises(ValueError, match="Invalid JSON"):
        nx.import_metadata(export_path)


def test_import_missing_required_fields(nx, temp_dir):
    """Test import with missing required fields."""
    # Create JSONL with missing required field
    export_path = temp_dir / "incomplete.jsonl"
    data = {"path": "/file.txt", "backend_name": "local"}  # Missing size, physical_path
    export_path.write_text(json.dumps(data) + "\n")

    # Should raise ValueError
    with pytest.raises(ValueError, match="Missing required field"):
        nx.import_metadata(export_path)


def test_import_file_not_found(nx, temp_dir):
    """Test import with non-existent file."""
    # Should raise FileNotFoundError
    with pytest.raises(FileNotFoundError):
        nx.import_metadata(temp_dir / "nonexistent.jsonl")


def test_export_import_round_trip(nx, temp_dir):
    """Test complete export/import round trip."""
    # Create diverse set of files
    nx.write("/file1.txt", b"content1")
    nx.write("/workspace/file2.txt", b"content2")
    nx.write("/workspace/data/file3.txt", b"content3")

    # Export
    export_path = temp_dir / "backup.jsonl"
    export_count = nx.export_metadata(export_path)
    assert export_count == 3

    # Store original metadata
    original_metas = {
        "/file1.txt": nx.metadata.get("/file1.txt"),
        "/workspace/file2.txt": nx.metadata.get("/workspace/file2.txt"),
        "/workspace/data/file3.txt": nx.metadata.get("/workspace/data/file3.txt"),
    }

    # Delete all files
    nx.delete("/file1.txt")
    nx.delete("/workspace/file2.txt")
    nx.delete("/workspace/data/file3.txt")

    # Verify deleted
    assert not nx.exists("/file1.txt")
    assert not nx.exists("/workspace/file2.txt")
    assert not nx.exists("/workspace/data/file3.txt")

    # Import
    result = nx.import_metadata(export_path)
    assert result.created == 3
    assert result.updated == 0
    assert result.skipped == 0

    # Verify all files restored with correct metadata
    for path, original_meta in original_metas.items():
        assert nx.exists(path)
        current_meta = nx.metadata.get(path)
        assert current_meta.etag == original_meta.etag
        assert current_meta.size == original_meta.size
