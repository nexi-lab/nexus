"""Test hierarchical directory listing with ReBAC permissions.

This test reproduces the bug where a user with permission to a subdirectory
(e.g., /workspace/joe) cannot see the parent directory (e.g., /workspace)
when listing the root directory.
"""

import pytest
from sqlalchemy import create_engine

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def nexus_fs(tmp_path):
    """Create a NexusFS instance with ReBAC enabled."""
    backend_path = tmp_path / "backend"
    backend_path.mkdir()
    db_path = str(tmp_path / "metadata.db")

    # Create LocalBackend instance
    backend = LocalBackend(root_path=str(backend_path))

    fs = NexusFS(
        backend=backend,
        db_path=db_path,
        enforce_permissions=True,
        allow_admin_bypass=True,  # Allow admin to create test setup
    )
    yield fs
    fs.close()


def test_hierarchical_directory_listing_empty_subdirectory(nexus_fs):
    """Test that parent directories appear when user has access to empty subdirectories.

    Bug scenario:
    1. Joe has permission to /workspace/joe but not /workspace
    2. /workspace/joe is empty (no files)
    3. When Joe runs `ls /`, /workspace should appear

    Expected: /workspace should be visible because Joe has access to /workspace/joe
    Actual (buggy): /workspace is not visible because /workspace/joe is empty
    """
    # Create directories as admin user (system has path restrictions)
    from nexus.core.permissions_enhanced import AdminCapability, EnhancedOperationContext

    system_ctx = EnhancedOperationContext(
        user="admin",
        groups=[],
        tenant_id="default",
        is_admin=True,
        is_system=False,
        subject_type="user",
        subject_id="admin",
        admin_capabilities={
            AdminCapability.WRITE_ALL,
            AdminCapability.READ_ALL,
        },  # Grant full admin access
    )

    # Create /workspace and /workspace/joe directories
    nexus_fs.mkdir("/workspace", parents=True, exist_ok=True, context=system_ctx)
    nexus_fs.mkdir("/workspace/joe", parents=True, exist_ok=True, context=system_ctx)

    # Set up permissions: Joe has read/list permission on /workspace/joe
    nexus_fs.rebac_create(
        subject=("agent", "joe"),
        relation="direct_viewer",  # Use direct_viewer (viewer is a union, not a base relation)
        object=("file", "/workspace/joe"),
        tenant_id="default",
        context=system_ctx,
    )

    # Create Joe's context (subject_type must match the ReBAC tuple)
    # Since we created tuple with subject=("agent", "joe"), context must have subject_type="agent"
    from nexus.core.permissions_enhanced import EnhancedOperationContext

    joe_ctx = EnhancedOperationContext(
        user="joe",
        groups=[],
        tenant_id="default",
        is_admin=False,
        is_system=False,
        subject_type="agent",  # Match the ReBAC tuple subject type
        subject_id="joe",
    )

    # List root directory as Joe
    result = nexus_fs.list("/", recursive=False, details=True, context=joe_ctx)

    # Extract directory paths
    directories = [item["path"] for item in result if item.get("is_directory")]

    # EXPECTED: Joe should see /workspace because he has access to /workspace/joe
    # ACTUAL (BUG): /workspace is missing if /workspace/joe is empty
    assert "/workspace" in directories, (
        f"Expected /workspace to appear in listing, but got: {directories}. "
        f"Joe has access to /workspace/joe, so parent /workspace should be visible."
    )


def test_hierarchical_directory_listing_with_files(nexus_fs):
    """Test that parent directories appear when user has access to subdirectories with files.

    This test verifies the CURRENT working behavior (when subdirectory has files).
    """
    # Create directories and file as admin user (system has path restrictions)
    from nexus.core.permissions_enhanced import AdminCapability, EnhancedOperationContext

    system_ctx = EnhancedOperationContext(
        user="admin",
        groups=[],
        tenant_id="default",
        is_admin=True,
        is_system=False,
        subject_type="user",
        subject_id="admin",
        admin_capabilities={
            AdminCapability.WRITE_ALL,
            AdminCapability.READ_ALL,
        },  # Grant full admin access
    )

    # Create /workspace/joe and a file inside
    nexus_fs.mkdir("/workspace/joe", parents=True, exist_ok=True, context=system_ctx)
    nexus_fs.write("/workspace/joe/file.txt", b"content", context=system_ctx)

    # Set up permissions: Joe has read permission on /workspace/joe/file.txt
    nexus_fs.rebac_create(
        subject=("agent", "joe"),
        relation="direct_viewer",
        object=("file", "/workspace/joe/file.txt"),
        tenant_id="default",
        context=system_ctx,
    )

    # Create Joe's context (subject_type must match the ReBAC tuple)
    # Since we created tuple with subject=("agent", "joe"), context must have subject_type="agent"
    from nexus.core.permissions_enhanced import EnhancedOperationContext

    joe_ctx = EnhancedOperationContext(
        user="joe",
        groups=[],
        tenant_id="default",
        is_admin=False,
        is_system=False,
        subject_type="agent",  # Match the ReBAC tuple subject type
        subject_id="joe",
    )

    # List root directory as Joe
    result = nexus_fs.list("/", recursive=False, details=True, context=joe_ctx)

    # Extract directory paths
    directories = [item["path"] for item in result if item.get("is_directory")]

    # This should work: Joe can see /workspace because /workspace/joe has files he can read
    assert "/workspace" in directories, (
        f"Expected /workspace to appear when subdirectory has readable files. Got: {directories}"
    )


def test_deeply_nested_hierarchical_listing(nexus_fs):
    """Test hierarchical listing with deeply nested directories.

    Scenario:
    - Joe has access to /a/b/c/d/joe but not parent directories
    - All intermediate directories should appear in their parent listings
    """
    # Create directories as admin user (system has path restrictions)
    from nexus.core.permissions_enhanced import AdminCapability, EnhancedOperationContext

    system_ctx = EnhancedOperationContext(
        user="admin",
        groups=[],
        tenant_id="default",
        is_admin=True,
        is_system=False,
        subject_type="user",
        subject_id="admin",
        admin_capabilities={
            AdminCapability.WRITE_ALL,
            AdminCapability.READ_ALL,
        },  # Grant full admin access
    )

    # Create deeply nested directory
    deep_path = "/a/b/c/d/joe"
    nexus_fs.mkdir(deep_path, parents=True, exist_ok=True, context=system_ctx)

    # Create a file in Joe's directory
    nexus_fs.write(f"{deep_path}/file.txt", b"content", context=system_ctx)

    # Grant Joe access to the file
    nexus_fs.rebac_create(
        subject=("agent", "joe"),
        relation="direct_viewer",
        object=("file", f"{deep_path}/file.txt"),
        tenant_id="default",
        context=system_ctx,
    )

    # Create Joe's context (subject_type must match the ReBAC tuple)
    # Since we created tuple with subject=("agent", "joe"), context must have subject_type="agent"
    from nexus.core.permissions_enhanced import EnhancedOperationContext

    joe_ctx = EnhancedOperationContext(
        user="joe",
        groups=[],
        tenant_id="default",
        is_admin=False,
        is_system=False,
        subject_type="agent",  # Match the ReBAC tuple subject type
        subject_id="joe",
    )

    # List each level
    # List /
    root_listing = nexus_fs.list("/", recursive=False, details=True, context=joe_ctx)
    root_dirs = [item["path"] for item in root_listing if item.get("is_directory")]
    assert "/a" in root_dirs, f"Expected /a in root listing, got: {root_dirs}"

    # List /a
    a_listing = nexus_fs.list("/a", recursive=False, details=True, context=joe_ctx)
    a_dirs = [item["path"] for item in a_listing if item.get("is_directory")]
    assert "/a/b" in a_dirs, f"Expected /a/b in /a listing, got: {a_dirs}"

    # List /a/b
    b_listing = nexus_fs.list("/a/b", recursive=False, details=True, context=joe_ctx)
    b_dirs = [item["path"] for item in b_listing if item.get("is_directory")]
    assert "/a/b/c" in b_dirs, f"Expected /a/b/c in /a/b listing, got: {b_dirs}"
