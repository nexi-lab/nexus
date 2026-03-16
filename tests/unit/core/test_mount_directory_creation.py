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

from nexus.core.config import PermissionConfig
from nexus.storage.raft_metadata_store import RaftMetadataStore


@pytest.fixture
async def nx_with_mount():
    """Create NexusFS instance with mount manager support via factory."""
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.factory import create_nexus_fs

    with tempfile.TemporaryDirectory() as tmpdir:
        root_backend = CASLocalBackend(root_path=tmpdir)
        db_file = Path(tmpdir) / "metadata.db"
        metadata_store = RaftMetadataStore.embedded(str(db_file).replace(".db", ""))

        nx = await create_nexus_fs(
            backend=root_backend,
            metadata_store=metadata_store,
            permissions=PermissionConfig(enforce=False),
        )

        yield nx, tmpdir

        nx.close()


@pytest.mark.asyncio
async def test_mount_creates_directory_entry(nx_with_mount):
    """Test that adding a mount creates directory metadata entry."""
    nx, tmpdir = nx_with_mount

    # Create a mount point
    mount_backend = MagicMock()
    mount_backend.name = "test_mount"

    # Add mount directly to router (simulating config-based mount)
    nx.router.add_mount("/mnt/test", mount_backend, readonly=False)

    # Create directory entry (this is what server.py now does)
    await nx.sys_mkdir("/mnt/test", parents=True, exist_ok=True)

    # Verify directory exists in metadata
    assert nx.metadata.exists("/mnt")
    assert nx.metadata.exists("/mnt/test")

    # Verify /mnt is recognized as a directory by the kernel
    assert await nx.sys_is_directory("/mnt")
    mnt_meta = nx.metadata.get("/mnt")
    assert mnt_meta is not None

    # Verify mount point is a DT_MOUNT entry (created by PathRouter.add_mount,
    # not overwritten by sys_mkdir which honours the existing entry) and is
    # recognized as a directory by the kernel.
    assert await nx.sys_is_directory("/mnt/test")
    test_meta = nx.metadata.get("/mnt/test")
    assert test_meta is not None
    assert test_meta.is_mount, f"Expected DT_MOUNT entry, got entry_type={test_meta.entry_type}"


@pytest.mark.asyncio
async def test_mount_appears_in_listing(nx_with_mount):
    """Test that mount points appear when listing parent directories."""
    nx, tmpdir = nx_with_mount

    # Create a mount point
    mount_backend = MagicMock()
    mount_backend.name = "test_mount"

    # Add mount and create directory
    nx.router.add_mount("/mnt/gcs_demo", mount_backend, readonly=False)
    await nx.sys_mkdir("/mnt/gcs_demo", parents=True, exist_ok=True)

    # List root directory (non-recursive)
    root_list = await nx.sys_readdir("/", recursive=False, details=False)

    # /mnt should appear in root listing
    assert "/mnt" in root_list, f"Expected /mnt in {root_list}"

    # List /mnt directory (non-recursive)
    mnt_list = await nx.sys_readdir("/mnt", recursive=False, details=False)

    # /mnt/gcs_demo should appear in /mnt listing
    assert "/mnt/gcs_demo" in mnt_list, f"Expected /mnt/gcs_demo in {mnt_list}"


@pytest.mark.asyncio
async def test_mount_appears_in_detailed_listing(nx_with_mount):
    """Test that mount points appear with correct metadata in detailed listings."""
    nx, tmpdir = nx_with_mount

    # Create a mount point
    mount_backend = MagicMock()
    mount_backend.name = "test_mount"

    # Add mount and create directory
    nx.router.add_mount("/personal/alice", mount_backend, readonly=False)
    await nx.sys_mkdir("/personal/alice", parents=True, exist_ok=True)

    # List with details
    root_list = await nx.sys_readdir("/", recursive=False, details=True)

    # Find /personal in results
    personal_entry = next((e for e in root_list if e["path"] == "/personal"), None)
    assert personal_entry is not None, f"Expected /personal in {root_list}"
    # sys_readdir(details=True) returns {"path", "size", "etag"}; verify keys
    assert "size" in personal_entry
    assert "etag" in personal_entry
    # Confirm the kernel recognises /personal as a directory
    assert await nx.sys_is_directory("/personal")

    # List /personal with details
    personal_list = await nx.sys_readdir("/personal", recursive=False, details=True)

    # Find /personal/alice in results
    alice_entry = next((e for e in personal_list if e["path"] == "/personal/alice"), None)
    assert alice_entry is not None, f"Expected /personal/alice in {personal_list}"
    assert "size" in alice_entry
    assert "etag" in alice_entry
    # Confirm the kernel recognises /personal/alice as a directory
    assert await nx.sys_is_directory("/personal/alice")


@pytest.mark.asyncio
async def test_nested_mount_creates_all_parents(nx_with_mount):
    """Test that mounting at /a/b/c/mount creates /a, /a/b, /a/b/c, /a/b/c/mount."""
    nx, tmpdir = nx_with_mount

    # Create a deeply nested mount
    mount_backend = MagicMock()
    mount_backend.name = "deep_mount"

    # Add mount and create directory with parents
    nx.router.add_mount("/a/b/c/mount", mount_backend, readonly=False)
    await nx.sys_mkdir("/a/b/c/mount", parents=True, exist_ok=True)

    # Verify all parents exist
    assert nx.metadata.exists("/a")
    assert nx.metadata.exists("/a/b")
    assert nx.metadata.exists("/a/b/c")
    assert nx.metadata.exists("/a/b/c/mount")

    # Verify all paths are recognized as directories by the kernel.
    # Parent directories are created by sys_mkdir, while the mount point
    # itself is a DT_MOUNT created by PathRouter.add_mount.  Both are
    # treated as directory-like by sys_is_directory.
    for p in ["/a", "/a/b", "/a/b/c", "/a/b/c/mount"]:
        assert await nx.sys_is_directory(p), f"Expected {p} to be a directory"

    # The mount point should be a DT_MOUNT entry
    mount_meta = nx.metadata.get("/a/b/c/mount")
    assert mount_meta is not None
    assert mount_meta.is_mount, f"Expected DT_MOUNT, got entry_type={mount_meta.entry_type}"


@pytest.mark.asyncio
async def test_sync_mount_ensures_directory_exists(nx_with_mount):
    """Test that sync_mount creates directory entry if missing."""
    nx, tmpdir = nx_with_mount

    # Create a local backend directory for the mount
    mount_dir = Path(tmpdir) / "mount_data"
    mount_dir.mkdir()
    (mount_dir / "test.txt").write_text("test content")

    from nexus.contracts.types import OperationContext

    # Create context with zone_id and admin access for the test user
    ctx = OperationContext(user_id="test-user", groups=[], zone_id="test", is_admin=True)

    # Use mount_core_service.add_mount (async) which properly grants permissions
    mount_point = await nx.service("mount_core").add_mount(
        mount_point="/zone/test/old/mount",
        backend_type="cas_local",
        backend_config={"data_dir": str(mount_dir)},
        readonly=False,
        context=ctx,
    )

    # Sync mount via sync_service (should ensure directory exists)
    from nexus.contracts.types import SyncContext

    sync_ctx = SyncContext(
        mount_point=mount_point,
        context=ctx,
    )
    result = nx.service("sync").sync_mount(sync_ctx)

    # Verify directory exists after sync
    assert nx.metadata.exists("/zone/test/old")
    assert nx.metadata.exists("/zone/test/old/mount")

    # Sync result should be returned (SyncResult dataclass)
    assert result.files_scanned >= 0


@pytest.mark.asyncio
async def test_add_mount_via_api_creates_directory(nx_with_mount):
    """Test that add_mount() API creates directory entry via _grant_mount_owner_permission."""
    nx, tmpdir = nx_with_mount

    # Create a backend directory
    mount_dir = Path(tmpdir) / "api_mount"
    mount_dir.mkdir()

    # Use mount_core_service.add_mount (async) instead of removed nx.add_mount
    mount_id = await nx.service("mount_core").add_mount(
        mount_point="/api/mount",
        backend_type="cas_local",
        backend_config={"data_dir": str(mount_dir)},
        readonly=False,
    )

    assert mount_id == "/api/mount"

    # Verify directory was created
    assert nx.metadata.exists("/api")
    assert nx.metadata.exists("/api/mount")

    # Verify mount appears in listing
    api_list = await nx.sys_readdir("/api", recursive=False, details=False)
    assert "/api/mount" in api_list


@pytest.mark.asyncio
async def test_mount_exist_ok_does_not_fail(nx_with_mount):
    """Test that creating mount directory with exist_ok=True doesn't fail if already exists."""
    nx, tmpdir = nx_with_mount

    # Create directory first
    await nx.sys_mkdir("/mnt/test", parents=True, exist_ok=True)

    # Create it again with exist_ok=True (should not raise)
    await nx.sys_mkdir("/mnt/test", parents=True, exist_ok=True)

    # Verify it still exists
    assert nx.metadata.exists("/mnt/test")


@pytest.mark.asyncio
async def test_multiple_mounts_in_same_parent(nx_with_mount):
    """Test that multiple mounts under same parent all appear in listing."""
    nx, tmpdir = nx_with_mount

    # Create multiple mounts under /mnt
    for name in ["mount1", "mount2", "mount3"]:
        mount_backend = MagicMock()
        mount_backend.name = name
        nx.router.add_mount(f"/mnt/{name}", mount_backend, readonly=False)
        await nx.sys_mkdir(f"/mnt/{name}", parents=True, exist_ok=True)

    # List /mnt
    mnt_list = await nx.sys_readdir("/mnt", recursive=False, details=False)

    # All mounts should appear
    assert "/mnt/mount1" in mnt_list
    assert "/mnt/mount2" in mnt_list
    assert "/mnt/mount3" in mnt_list
