"""Tests for workspace snapshot and versioning functionality."""

import json
import tempfile
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.exceptions import NexusFileNotFoundError


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_workspace):
    """Create a NexusFS instance with workspace versioning."""
    backend = LocalBackend(temp_workspace / "storage")
    fs = NexusFS(
        backend=backend,
        db_path=temp_workspace / "metadata.db",
        agent_id="test-agent",
        tenant_id="test-tenant",
        auto_parse=False,
    )
    yield fs
    fs.close()


def test_create_snapshot_empty_workspace(nx):
    """Test creating a snapshot of an empty workspace."""
    snapshot = nx.workspace_snapshot(description="Empty workspace")

    assert snapshot["snapshot_number"] == 1
    assert snapshot["file_count"] == 0
    assert snapshot["total_size_bytes"] == 0
    assert snapshot["description"] == "Empty workspace"
    assert "snapshot_id" in snapshot
    assert "manifest_hash" in snapshot
    assert "created_at" in snapshot


def test_create_snapshot_with_files(nx):
    """Test creating a snapshot with files."""
    # Create files in workspace
    nx.write("/workspace/test-tenant/test-agent/file1.txt", b"Hello World")
    nx.write("/workspace/test-tenant/test-agent/file2.txt", b"Goodbye World")
    nx.write("/workspace/test-tenant/test-agent/data/file3.json", b'{"key": "value"}')

    snapshot = nx.workspace_snapshot(description="Three files")

    assert snapshot["snapshot_number"] == 1
    assert snapshot["file_count"] == 3
    assert snapshot["total_size_bytes"] > 0
    assert snapshot["description"] == "Three files"


def test_create_multiple_snapshots(nx):
    """Test creating multiple sequential snapshots."""
    # Snapshot 1: Empty
    snap1 = nx.workspace_snapshot(description="Snapshot 1")
    assert snap1["snapshot_number"] == 1

    # Add file and create snapshot 2
    nx.write("/workspace/test-tenant/test-agent/file1.txt", b"Hello")
    snap2 = nx.workspace_snapshot(description="Snapshot 2")
    assert snap2["snapshot_number"] == 2
    assert snap2["file_count"] == 1

    # Add another file and create snapshot 3
    nx.write("/workspace/test-tenant/test-agent/file2.txt", b"World")
    snap3 = nx.workspace_snapshot(description="Snapshot 3")
    assert snap3["snapshot_number"] == 3
    assert snap3["file_count"] == 2


def test_snapshot_with_tags(nx):
    """Test creating snapshots with tags."""
    nx.write("/workspace/test-tenant/test-agent/file.txt", b"Data")

    snapshot = nx.workspace_snapshot(
        description="Tagged snapshot",
        tags=["experiment", "v1.0", "important"],
    )

    assert snapshot["tags"] == ["experiment", "v1.0", "important"]


def test_workspace_log(nx):
    """Test listing workspace snapshot history."""
    # Create multiple snapshots
    nx.workspace_snapshot(description="Snapshot 1")
    nx.write("/workspace/test-tenant/test-agent/file.txt", b"Data")
    nx.workspace_snapshot(description="Snapshot 2")
    nx.write("/workspace/test-tenant/test-agent/file2.txt", b"More data")
    nx.workspace_snapshot(description="Snapshot 3", tags=["final"])

    # Get log
    snapshots = nx.workspace_log(limit=10)

    assert len(snapshots) == 3
    # Most recent first
    assert snapshots[0]["snapshot_number"] == 3
    assert snapshots[0]["description"] == "Snapshot 3"
    assert snapshots[0]["tags"] == ["final"]
    assert snapshots[1]["snapshot_number"] == 2
    assert snapshots[2]["snapshot_number"] == 1


def test_workspace_log_limit(nx):
    """Test workspace log with limit."""
    # Create 10 snapshots
    for i in range(10):
        nx.workspace_snapshot(description=f"Snapshot {i + 1}")

    # Get only 5
    snapshots = nx.workspace_log(limit=5)
    assert len(snapshots) == 5
    # Most recent first (10, 9, 8, 7, 6)
    assert snapshots[0]["snapshot_number"] == 10
    assert snapshots[4]["snapshot_number"] == 6


def test_restore_snapshot(nx):
    """Test restoring workspace to previous snapshot."""
    # Create initial files
    nx.write("/workspace/test-tenant/test-agent/file1.txt", b"Version 1")
    nx.write("/workspace/test-tenant/test-agent/file2.txt", b"Data 2")
    nx.workspace_snapshot(description="Two files")

    # Modify files
    nx.write("/workspace/test-tenant/test-agent/file1.txt", b"Version 2")
    nx.write("/workspace/test-tenant/test-agent/file3.txt", b"New file")
    nx.workspace_snapshot(description="Modified")

    # Verify current state
    assert nx.read("/workspace/test-tenant/test-agent/file1.txt") == b"Version 2"
    assert nx.exists("/workspace/test-tenant/test-agent/file3.txt")

    # Restore to snapshot 1
    result = nx.workspace_restore(snapshot_number=1)

    assert result["files_restored"] > 0
    assert result["files_deleted"] > 0

    # Verify restored state
    assert nx.read("/workspace/test-tenant/test-agent/file1.txt") == b"Version 1"
    assert nx.read("/workspace/test-tenant/test-agent/file2.txt") == b"Data 2"
    assert not nx.exists("/workspace/test-tenant/test-agent/file3.txt")


def test_restore_nonexistent_snapshot(nx):
    """Test restoring nonexistent snapshot raises error."""
    with pytest.raises(NexusFileNotFoundError):
        nx.workspace_restore(snapshot_number=999)


def test_diff_snapshots(nx):
    """Test comparing two snapshots."""
    # Snapshot 1: Initial files
    nx.write("/workspace/test-tenant/test-agent/file1.txt", b"Data 1")
    nx.write("/workspace/test-tenant/test-agent/file2.txt", b"Data 2")
    nx.workspace_snapshot(description="Initial")

    # Snapshot 2: Add, remove, modify
    nx.write("/workspace/test-tenant/test-agent/file1.txt", b"Modified Data 1")  # Modified
    nx.delete("/workspace/test-tenant/test-agent/file2.txt")  # Removed
    nx.write("/workspace/test-tenant/test-agent/file3.txt", b"New file")  # Added
    nx.workspace_snapshot(description="Modified")

    # Compute diff
    diff = nx.workspace_diff(snapshot_1=1, snapshot_2=2)

    assert diff["snapshot_1"]["snapshot_number"] == 1
    assert diff["snapshot_2"]["snapshot_number"] == 2

    # Check added files
    added_paths = [f["path"] for f in diff["added"]]
    assert "file3.txt" in added_paths

    # Check removed files
    removed_paths = [f["path"] for f in diff["removed"]]
    assert "file2.txt" in removed_paths

    # Check modified files
    modified_paths = [f["path"] for f in diff["modified"]]
    assert "file1.txt" in modified_paths


def test_diff_identical_snapshots(nx):
    """Test diff of identical snapshots."""
    nx.write("/workspace/test-tenant/test-agent/file.txt", b"Data")
    nx.workspace_snapshot()

    # Create identical snapshot (same files)
    nx.workspace_snapshot()

    diff = nx.workspace_diff(snapshot_1=1, snapshot_2=2)

    assert len(diff["added"]) == 0
    assert len(diff["removed"]) == 0
    assert len(diff["modified"]) == 0
    assert diff["unchanged"] == 1


def test_diff_nonexistent_snapshots(nx):
    """Test diff with nonexistent snapshots."""
    nx.workspace_snapshot()

    with pytest.raises(NexusFileNotFoundError):
        nx.workspace_diff(snapshot_1=1, snapshot_2=999)

    with pytest.raises(NexusFileNotFoundError):
        nx.workspace_diff(snapshot_1=999, snapshot_2=1)


def test_snapshot_deduplication(nx):
    """Test that identical workspace states produce same manifest hash."""
    # Create workspace state
    nx.write("/workspace/test-tenant/test-agent/file1.txt", b"Data 1")
    nx.write("/workspace/test-tenant/test-agent/file2.txt", b"Data 2")
    snap1 = nx.workspace_snapshot()

    # Restore and re-snapshot
    nx.workspace_restore(snapshot_number=1)
    snap2 = nx.workspace_snapshot()

    # Same manifest hash due to deduplication
    assert snap1["manifest_hash"] == snap2["manifest_hash"]


def test_snapshot_without_agent_id(temp_workspace):
    """Test snapshot fails without agent_id."""
    backend = LocalBackend(temp_workspace / "storage")
    nx = NexusFS(
        backend=backend,
        db_path=temp_workspace / "metadata.db",
        auto_parse=False,
    )

    with pytest.raises(ValueError, match="agent_id must be provided"):
        nx.workspace_snapshot()

    nx.close()


def test_snapshot_only_workspace_files(nx):
    """Test that snapshots only include workspace files, not other namespaces."""
    # Write to workspace
    nx.write("/workspace/test-tenant/test-agent/workspace-file.txt", b"Workspace")

    # Write to shared (different namespace)
    nx.write("/shared/test-tenant/shared-file.txt", b"Shared")

    # Create snapshot
    snapshot = nx.workspace_snapshot()

    # Should only count workspace files
    assert snapshot["file_count"] == 1


def test_restore_preserves_content_deduplication(nx):
    """Test that restore maintains CAS content deduplication."""
    # Create file with content
    content = b"Hello World" * 1000
    nx.write("/workspace/test-tenant/test-agent/file.txt", content)
    nx.workspace_snapshot()

    # Get content hash
    meta1 = nx.metadata.get("/workspace/test-tenant/test-agent/file.txt")
    hash1 = meta1.etag

    # Delete and restore
    nx.delete("/workspace/test-tenant/test-agent/file.txt")
    nx.workspace_restore(snapshot_number=1)

    # Content hash should be identical (same CAS content)
    meta2 = nx.metadata.get("/workspace/test-tenant/test-agent/file.txt")
    hash2 = meta2.etag

    assert hash1 == hash2
    assert nx.read("/workspace/test-tenant/test-agent/file.txt") == content


def test_large_workspace_snapshot(nx):
    """Test snapshot performance with many files."""
    # Create 100 files
    for i in range(100):
        nx.write(f"/workspace/test-tenant/test-agent/file{i}.txt", f"Content {i}".encode())

    snapshot = nx.workspace_snapshot(description="100 files")

    assert snapshot["file_count"] == 100
    assert snapshot["total_size_bytes"] > 0


def test_nested_directory_snapshot(nx):
    """Test snapshot with nested directory structure."""
    # Create nested structure
    nx.write("/workspace/test-tenant/test-agent/dir1/file1.txt", b"Data 1")
    nx.write("/workspace/test-tenant/test-agent/dir1/dir2/file2.txt", b"Data 2")
    nx.write("/workspace/test-tenant/test-agent/dir1/dir2/dir3/file3.txt", b"Data 3")

    snap1 = nx.workspace_snapshot(description="Nested")
    assert snap1["file_count"] == 3

    # Restore and verify structure
    nx.delete("/workspace/test-tenant/test-agent/dir1/dir2/file2.txt")
    nx.workspace_restore(snapshot_number=1)

    assert nx.exists("/workspace/test-tenant/test-agent/dir1/file1.txt")
    assert nx.exists("/workspace/test-tenant/test-agent/dir1/dir2/file2.txt")
    assert nx.exists("/workspace/test-tenant/test-agent/dir1/dir2/dir3/file3.txt")


def test_manifest_stored_in_cas(nx, temp_workspace):
    """Test that snapshot manifest is stored in CAS."""
    nx.write("/workspace/test-tenant/test-agent/file.txt", b"Data")
    snapshot = nx.workspace_snapshot()

    manifest_hash = snapshot["manifest_hash"]

    # Read manifest from CAS
    backend = LocalBackend(temp_workspace / "storage")
    manifest_bytes = backend.read_content(manifest_hash)
    manifest = json.loads(manifest_bytes.decode("utf-8"))

    # Verify manifest structure
    assert isinstance(manifest, dict)
    assert "file.txt" in manifest
    assert "hash" in manifest["file.txt"]
    assert "size" in manifest["file.txt"]
