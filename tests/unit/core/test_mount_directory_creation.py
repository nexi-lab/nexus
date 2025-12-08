"""Unit tests for mount point directory creation.

This module tests that mount points (and their parent directories) are created
as actual metadata entries so they appear when listing parent paths.

For example:
- When mounting at /mnt/gcs_demo, both /mnt and /mnt/gcs_demo should appear
- Listing / should show /mnt as a directory
- Listing /mnt should show /mnt/gcs_demo as a directory
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def nx_with_mount():
    """Create NexusFS instance with mount manager support."""
    from nexus import NexusFS
    from nexus.backends.local import LocalBackend

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create root backend
        root_backend = LocalBackend(root_path=tmpdir)

        # Create NexusFS with metadata store
        nx = NexusFS(backend=root_backend, enforce_permissions=False)

        yield nx, tmpdir

        nx.close()


def test_mount_creates_directory_entry(nx_with_mount):
    """Test that adding a mount creates directory metadata entry."""
    nx, tmpdir = nx_with_mount

    # Create a mount point
    mount_backend = MagicMock()
    mount_backend.name = "test_mount"

    # Add mount directly to router (simulating config-based mount)
    nx.router.add_mount("/mnt/test", mount_backend, priority=0, readonly=False)

    # Create directory entry (this is what server.py now does)
    nx.mkdir("/mnt/test", parents=True, exist_ok=True)

    # Verify directory exists in metadata
    assert nx.metadata.exists("/mnt")
    assert nx.metadata.exists("/mnt/test")

    # Verify directories have correct MIME type
    mnt_meta = nx.metadata.get("/mnt")
    assert mnt_meta is not None
    assert mnt_meta.mime_type == "inode/directory"

    test_meta = nx.metadata.get("/mnt/test")
    assert test_meta is not None
    assert test_meta.mime_type == "inode/directory"


def test_mount_appears_in_listing(nx_with_mount):
    """Test that mount points appear when listing parent directories."""
    nx, tmpdir = nx_with_mount

    # Create a mount point
    mount_backend = MagicMock()
    mount_backend.name = "test_mount"

    # Add mount and create directory
    nx.router.add_mount("/mnt/gcs_demo", mount_backend, priority=0, readonly=False)
    nx.mkdir("/mnt/gcs_demo", parents=True, exist_ok=True)

    # List root directory (non-recursive)
    root_list = nx.list("/", recursive=False, details=False)

    # /mnt should appear in root listing
    assert "/mnt" in root_list, f"Expected /mnt in {root_list}"

    # List /mnt directory (non-recursive)
    mnt_list = nx.list("/mnt", recursive=False, details=False)

    # /mnt/gcs_demo should appear in /mnt listing
    assert "/mnt/gcs_demo" in mnt_list, f"Expected /mnt/gcs_demo in {mnt_list}"


def test_mount_appears_in_detailed_listing(nx_with_mount):
    """Test that mount points appear with correct metadata in detailed listings."""
    nx, tmpdir = nx_with_mount

    # Create a mount point
    mount_backend = MagicMock()
    mount_backend.name = "test_mount"

    # Add mount and create directory
    nx.router.add_mount("/personal/alice", mount_backend, priority=0, readonly=False)
    nx.mkdir("/personal/alice", parents=True, exist_ok=True)

    # List with details
    root_list = nx.list("/", recursive=False, details=True)

    # Find /personal in results
    personal_entry = next((e for e in root_list if e["path"] == "/personal"), None)
    assert personal_entry is not None, f"Expected /personal in {root_list}"
    assert personal_entry["is_directory"] is True

    # List /personal with details
    personal_list = nx.list("/personal", recursive=False, details=True)

    # Find /personal/alice in results
    alice_entry = next((e for e in personal_list if e["path"] == "/personal/alice"), None)
    assert alice_entry is not None, f"Expected /personal/alice in {personal_list}"
    assert alice_entry["is_directory"] is True


def test_nested_mount_creates_all_parents(nx_with_mount):
    """Test that mounting at /a/b/c/mount creates /a, /a/b, /a/b/c, /a/b/c/mount."""
    nx, tmpdir = nx_with_mount

    # Create a deeply nested mount
    mount_backend = MagicMock()
    mount_backend.name = "deep_mount"

    # Add mount and create directory with parents
    nx.router.add_mount("/a/b/c/mount", mount_backend, priority=0, readonly=False)
    nx.mkdir("/a/b/c/mount", parents=True, exist_ok=True)

    # Verify all parents exist
    assert nx.metadata.exists("/a")
    assert nx.metadata.exists("/a/b")
    assert nx.metadata.exists("/a/b/c")
    assert nx.metadata.exists("/a/b/c/mount")

    # Verify they all have directory MIME type
    for path in ["/a", "/a/b", "/a/b/c", "/a/b/c/mount"]:
        meta = nx.metadata.get(path)
        assert meta is not None, f"Expected metadata for {path}"
        assert meta.mime_type == "inode/directory", f"Expected directory type for {path}"


def test_sync_mount_ensures_directory_exists(nx_with_mount):
    """Test that sync_mount creates directory entry if missing (backwards compatibility)."""
    nx, tmpdir = nx_with_mount

    # Create a local backend for the mount
    mount_dir = Path(tmpdir) / "mount_data"
    mount_dir.mkdir()
    (mount_dir / "test.txt").write_text("test content")

    from nexus.backends.local import LocalBackend

    mount_backend = LocalBackend(root_path=str(mount_dir))

    # Add mount WITHOUT creating directory (simulating old behavior)
    nx.router.add_mount("/old/mount", mount_backend, priority=0, readonly=False)

    # At this point, the directory may or may not exist (depends on implementation)
    # The key test is that sync_mount ensures it exists

    # Sync mount (should ensure directory exists)
    result = nx.sync_mount("/old/mount")

    # Verify directory exists after sync
    assert nx.metadata.exists("/old")
    assert nx.metadata.exists("/old/mount")

    # Sync result should be returned (files_scanned may be 0 if backend is empty)
    assert "files_scanned" in result


def test_add_mount_via_api_creates_directory(nx_with_mount):
    """Test that add_mount() API creates directory entry via _grant_mount_owner_permission."""
    nx, tmpdir = nx_with_mount

    # Create a backend directory
    mount_dir = Path(tmpdir) / "api_mount"
    mount_dir.mkdir()

    # Use add_mount API (not router.add_mount)
    mount_id = nx.add_mount(
        mount_point="/api/mount",
        backend_type="local",
        backend_config={"data_dir": str(mount_dir)},
        priority=5,
        readonly=False,
    )

    assert mount_id == "/api/mount"

    # Verify directory was created
    assert nx.metadata.exists("/api")
    assert nx.metadata.exists("/api/mount")

    # Verify mount appears in listing
    api_list = nx.list("/api", recursive=False, details=False)
    assert "/api/mount" in api_list


def test_mount_exist_ok_does_not_fail(nx_with_mount):
    """Test that creating mount directory with exist_ok=True doesn't fail if already exists."""
    nx, tmpdir = nx_with_mount

    # Create directory first
    nx.mkdir("/mnt/test", parents=True, exist_ok=True)

    # Create it again with exist_ok=True (should not raise)
    nx.mkdir("/mnt/test", parents=True, exist_ok=True)

    # Verify it still exists
    assert nx.metadata.exists("/mnt/test")


def test_multiple_mounts_in_same_parent(nx_with_mount):
    """Test that multiple mounts under same parent all appear in listing."""
    nx, tmpdir = nx_with_mount

    # Create multiple mounts under /mnt
    for name in ["mount1", "mount2", "mount3"]:
        mount_backend = MagicMock()
        mount_backend.name = name
        nx.router.add_mount(f"/mnt/{name}", mount_backend, priority=0, readonly=False)
        nx.mkdir(f"/mnt/{name}", parents=True, exist_ok=True)

    # List /mnt
    mnt_list = nx.list("/mnt", recursive=False, details=False)

    # All mounts should appear
    assert "/mnt/mount1" in mnt_list
    assert "/mnt/mount2" in mnt_list
    assert "/mnt/mount3" in mnt_list
