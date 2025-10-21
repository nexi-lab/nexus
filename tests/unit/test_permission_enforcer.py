"""Tests for PermissionEnforcer and OperationContext classes."""

import pytest

from nexus.core.metadata import FileMetadata
from nexus.core.permissions import (
    OperationContext,
    Permission,
    PermissionEnforcer,
)


class TestOperationContext:
    """Tests for OperationContext dataclass."""

    def test_create_regular_user_context(self):
        """Test creating a regular user context."""
        ctx = OperationContext(user="alice", groups=["developers"])
        assert ctx.user == "alice"
        assert ctx.groups == ["developers"]
        assert ctx.is_admin is False
        assert ctx.is_system is False

    def test_create_admin_context(self):
        """Test creating an admin context."""
        ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)
        assert ctx.user == "admin"
        assert ctx.groups == ["admins"]
        assert ctx.is_admin is True
        assert ctx.is_system is False

    def test_create_system_context(self):
        """Test creating a system context."""
        ctx = OperationContext(user="system", groups=[], is_system=True)
        assert ctx.user == "system"
        assert ctx.groups == []
        assert ctx.is_admin is False
        assert ctx.is_system is True

    def test_requires_user(self):
        """Test that user is required."""
        with pytest.raises(ValueError, match="user is required"):
            OperationContext(user="", groups=[])

    def test_requires_groups_list(self):
        """Test that groups must be a list."""
        with pytest.raises(TypeError, match="groups must be list"):
            OperationContext(user="alice", groups="developers")  # type: ignore

    def test_empty_groups_allowed(self):
        """Test that empty groups list is allowed."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.groups == []


class TestPermissionEnforcer:
    """Tests for PermissionEnforcer class."""

    def test_admin_bypass(self):
        """Test that admin users bypass all checks."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="admin", groups=[], is_admin=True)

        # Admin can access anything
        assert enforcer.check("/any/path", Permission.READ, ctx) is True
        assert enforcer.check("/any/path", Permission.WRITE, ctx) is True
        assert enforcer.check("/any/path", Permission.EXECUTE, ctx) is True

    def test_system_bypass(self):
        """Test that system operations bypass all checks."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # System can access anything
        assert enforcer.check("/any/path", Permission.READ, ctx) is True
        assert enforcer.check("/any/path", Permission.WRITE, ctx) is True
        assert enforcer.check("/any/path", Permission.EXECUTE, ctx) is True

    def test_no_metadata_store_allows_all(self):
        """Test that without metadata store, all access is allowed (backward compat)."""
        enforcer = PermissionEnforcer(metadata_store=None)
        ctx = OperationContext(user="alice", groups=["developers"])

        # Without metadata store, allow everything
        assert enforcer.check("/any/path", Permission.READ, ctx) is True
        assert enforcer.check("/any/path", Permission.WRITE, ctx) is True

    def test_file_not_found_denies_access(self):
        """Test that non-existent files deny access."""

        class MockMetadataStore:
            def get(self, path: str):
                return None  # File doesn't exist

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="alice", groups=["developers"])

        # File doesn't exist - deny
        assert enforcer.check("/nonexistent", Permission.READ, ctx) is False

    def test_no_permissions_set_allows_all(self):
        """Test that files without permissions allow all access (backward compat)."""

        class MockMetadataStore:
            def get(self, path: str):
                # Return file metadata without permissions
                return FileMetadata(
                    path=path,
                    backend_name="local",
                    physical_path="/tmp/test",
                    size=100,
                    owner=None,  # No owner
                    group=None,  # No group
                    mode=None,  # No mode
                )

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="alice", groups=["developers"])

        # No permissions set - allow (backward compatibility)
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is True

    def test_owner_can_read_write(self):
        """Test that owner can read and write with 0o644 permissions."""

        class MockMetadataStore:
            def get(self, path: str):
                return FileMetadata(
                    path=path,
                    backend_name="local",
                    physical_path="/tmp/test",
                    size=100,
                    owner="alice",
                    group="developers",
                    mode=0o644,  # rw-r--r--
                )

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="alice", groups=["developers"])

        # Owner can read and write
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is True
        # But not execute (no x bit)
        assert enforcer.check("/file.txt", Permission.EXECUTE, ctx) is False

    def test_group_can_read_only(self):
        """Test that group members can only read with 0o644 permissions."""

        class MockMetadataStore:
            def get(self, path: str):
                return FileMetadata(
                    path=path,
                    backend_name="local",
                    physical_path="/tmp/test",
                    size=100,
                    owner="alice",
                    group="developers",
                    mode=0o644,  # rw-r--r--
                )

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="bob", groups=["developers"])

        # Group member can read
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        # But not write
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is False
        # And not execute
        assert enforcer.check("/file.txt", Permission.EXECUTE, ctx) is False

    def test_other_can_read_only(self):
        """Test that others can only read with 0o644 permissions."""

        class MockMetadataStore:
            def get(self, path: str):
                return FileMetadata(
                    path=path,
                    backend_name="local",
                    physical_path="/tmp/test",
                    size=100,
                    owner="alice",
                    group="developers",
                    mode=0o644,  # rw-r--r--
                )

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="charlie", groups=["designers"])

        # Other users can read
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        # But not write
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is False
        # And not execute
        assert enforcer.check("/file.txt", Permission.EXECUTE, ctx) is False

    def test_no_permissions_for_others_with_0o700(self):
        """Test that others have no access with 0o700 permissions."""

        class MockMetadataStore:
            def get(self, path: str):
                return FileMetadata(
                    path=path,
                    backend_name="local",
                    physical_path="/tmp/test",
                    size=100,
                    owner="alice",
                    group="developers",
                    mode=0o700,  # rwx------
                )

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="charlie", groups=["designers"])

        # Other users have no access
        assert enforcer.check("/secret.txt", Permission.READ, ctx) is False
        assert enforcer.check("/secret.txt", Permission.WRITE, ctx) is False
        assert enforcer.check("/secret.txt", Permission.EXECUTE, ctx) is False

    def test_execute_permission(self):
        """Test execute permission checking with 0o755."""

        class MockMetadataStore:
            def get(self, path: str):
                return FileMetadata(
                    path=path,
                    backend_name="local",
                    physical_path="/tmp/test",
                    size=100,
                    owner="alice",
                    group="developers",
                    mode=0o755,  # rwxr-xr-x
                )

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())

        # Owner can execute
        ctx_owner = OperationContext(user="alice", groups=["developers"])
        assert enforcer.check("/script.sh", Permission.EXECUTE, ctx_owner) is True

        # Group can execute
        ctx_group = OperationContext(user="bob", groups=["developers"])
        assert enforcer.check("/script.sh", Permission.EXECUTE, ctx_group) is True

        # Others can execute
        ctx_other = OperationContext(user="charlie", groups=["designers"])
        assert enforcer.check("/script.sh", Permission.EXECUTE, ctx_other) is True

    def test_filter_list_admin_sees_all(self):
        """Test that admins see all files in filter_list."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="admin", groups=[], is_admin=True)

        paths = ["/file1.txt", "/file2.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        # Admin sees all files
        assert filtered == paths

    def test_filter_list_system_sees_all(self):
        """Test that system context sees all files in filter_list."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)

        paths = ["/file1.txt", "/file2.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        # System sees all files
        assert filtered == paths

    def test_filter_list_filters_by_permission(self):
        """Test that filter_list removes files user can't read."""

        class MockMetadataStore:
            def __init__(self):
                self.files = {
                    "/public.txt": FileMetadata(
                        path="/public.txt",
                        backend_name="local",
                        physical_path="/tmp/public",
                        size=100,
                        owner="alice",
                        group="developers",
                        mode=0o644,  # rw-r--r-- (everyone can read)
                    ),
                    "/secret.txt": FileMetadata(
                        path="/secret.txt",
                        backend_name="local",
                        physical_path="/tmp/secret",
                        size=100,
                        owner="alice",
                        group="developers",
                        mode=0o600,  # rw------- (only owner can read)
                    ),
                }

            def get(self, path: str):
                return self.files.get(path)

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="bob", groups=["designers"])

        paths = ["/public.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        # Bob can only see public.txt
        assert filtered == ["/public.txt"]

    def test_invalid_permissions_deny_access(self):
        """Test that invalid permissions deny access."""

        class MockMetadataStore:
            def get(self, path: str):
                return FileMetadata(
                    path=path,
                    backend_name="local",
                    physical_path="/tmp/test",
                    size=100,
                    owner="alice",
                    group="developers",
                    mode=9999,  # Invalid mode
                )

        enforcer = PermissionEnforcer(metadata_store=MockMetadataStore())
        ctx = OperationContext(user="alice", groups=["developers"])

        # Invalid permissions - deny
        assert enforcer.check("/file.txt", Permission.READ, ctx) is False

    def test_rebac_stub_returns_false(self):
        """Test that ReBAC stub always returns False."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="alice", groups=["developers"])

        # ReBAC not implemented yet - always returns False
        assert enforcer._check_rebac("/file.txt", Permission.READ, ctx) is False

    def test_acl_stub_returns_none(self):
        """Test that ACL stub always returns None."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="alice", groups=["developers"])

        # ACL not implemented yet - always returns None
        assert enforcer._check_acl("/file.txt", Permission.READ, ctx) is None
