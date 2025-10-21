"""Integration tests for permission policy with NexusFS."""

import tempfile
from pathlib import Path

import pytest

from nexus import NexusFS
from nexus.backends.local import LocalBackend


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx_fs(temp_dir):
    """Create NexusFS instance with temporary storage."""
    backend = LocalBackend(str(temp_dir / "storage"))
    db_path = temp_dir / "metadata.db"

    fs = NexusFS(
        backend=backend,
        db_path=db_path,
        tenant_id="test-tenant",
        agent_id="alice",
    )

    yield fs

    fs.close()


class TestPermissionPolicyIntegration:
    """Integration tests for permission policies with NexusFS."""

    def test_write_new_file_applies_workspace_policy(self, nx_fs):
        """Test that writing a new file in /workspace applies the workspace policy."""
        # Write a new file
        # Use full path format: /workspace/{tenant_id}/{agent_id}/file.txt
        path = "/workspace/test-tenant/alice/test.txt"
        nx_fs.write(path, b"Hello World")

        # Get metadata
        meta = nx_fs.metadata.get(path)

        # Check that permissions were applied
        assert meta.owner == "alice"  # ${agent_id} substituted
        assert meta.group == "agents"
        assert meta.mode == 0o644  # rw-r--r--

    def test_write_new_file_applies_shared_policy(self, nx_fs):
        """Test that writing a new file in /shared applies the shared policy."""
        # Write a new file
        path = "/shared/test-tenant/data.txt"
        nx_fs.write(path, b"Shared Data")

        # Get metadata
        meta = nx_fs.metadata.get(path)

        # Check that permissions were applied
        assert meta.owner == "root"
        assert meta.group == "test-tenant"  # ${tenant_id} substituted
        assert meta.mode == 0o664  # rw-rw-r--

    # NOTE: Tests for /archives and /system policies are skipped because those namespaces
    # have router-level access restrictions (read-only, admin-only) that prevent writes.
    # The policy system itself works correctly - it's just blocked by the router.
    # To test those policies, you would need to modify the router or test at a lower level.

    def test_update_existing_file_preserves_permissions(self, nx_fs):
        """Test that updating an existing file preserves its permissions."""
        # Write initial file
        path = "/workspace/test-tenant/alice/test.txt"
        nx_fs.write(path, b"Initial")

        # Get initial metadata
        meta1 = nx_fs.metadata.get(path)
        initial_owner = meta1.owner
        initial_group = meta1.group
        initial_mode = meta1.mode

        # Update the file
        nx_fs.write(path, b"Updated")

        # Get updated metadata
        meta2 = nx_fs.metadata.get(path)

        # Permissions should be preserved (not re-applied)
        assert meta2.owner == initial_owner
        assert meta2.group == initial_group
        assert meta2.mode == initial_mode

    def test_no_policy_match_no_permissions(self, nx_fs):
        """Test that files without matching policy have no permissions set."""
        # Write to a path that doesn't match any default policy
        path = "/custom/namespace/file.txt"
        nx_fs.write(path, b"Data")

        # Get metadata
        meta = nx_fs.metadata.get(path)

        # No permissions should be set
        assert meta.owner is None
        assert meta.group is None
        assert meta.mode is None

    def test_multiple_files_same_policy(self, nx_fs):
        """Test that multiple files in the same namespace get the same policy."""
        # Write multiple files in /workspace
        paths = [
            "/workspace/test-tenant/alice/file1.txt",
            "/workspace/test-tenant/alice/file2.txt",
            "/workspace/test-tenant/alice/file3.txt",
        ]
        nx_fs.write(paths[0], b"File 1")
        nx_fs.write(paths[1], b"File 2")
        nx_fs.write(paths[2], b"File 3")

        # All should have the same permissions
        for path in paths:
            meta = nx_fs.metadata.get(path)
            assert meta.owner == "alice"
            assert meta.group == "agents"
            assert meta.mode == 0o644

    def test_different_agents_different_ownership(self):
        """Test that different agents get different ownership."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            backend = LocalBackend(str(tmpdir / "storage"))

            # Create FS for alice
            db_path = tmpdir / "metadata.db"
            fs_alice = NexusFS(
                backend=backend,
                db_path=db_path,
                tenant_id="test-tenant",
                agent_id="alice",
            )

            # Write as alice
            path_alice = "/workspace/test-tenant/alice/alice-file.txt"
            fs_alice.write(path_alice, b"Alice's file")
            meta_alice = fs_alice.metadata.get(path_alice)

            fs_alice.close()

            # Create FS for bob (reusing same backend and db)
            fs_bob = NexusFS(
                backend=backend,
                db_path=db_path,
                tenant_id="test-tenant",
                agent_id="bob",
            )

            # Write as bob
            path_bob = "/workspace/test-tenant/bob/bob-file.txt"
            fs_bob.write(path_bob, b"Bob's file")
            meta_bob = fs_bob.metadata.get(path_bob)

            # Check ownership
            assert meta_alice.owner == "alice"
            assert meta_bob.owner == "bob"

            fs_bob.close()

    def test_policy_with_nested_paths(self, nx_fs):
        """Test that policies work with nested paths."""
        # Write to nested paths
        path1 = "/workspace/test-tenant/alice/subdir/file.txt"
        path2 = "/shared/test-tenant/team/project/data.json"

        nx_fs.write(path1, b"Nested")
        nx_fs.write(path2, b"Data")

        # Check workspace nested file
        meta1 = nx_fs.metadata.get(path1)
        assert meta1.owner == "alice"
        assert meta1.group == "agents"
        assert meta1.mode == 0o644

        # Check shared nested file
        meta2 = nx_fs.metadata.get(path2)
        assert meta2.owner == "root"
        assert meta2.group == "test-tenant"
        assert meta2.mode == 0o664
