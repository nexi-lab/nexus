"""End-to-end tests for share permission security improvements (Issues #817 and #818).

Tests cover:
- Issue #817: Security checks in share_with_user()
- Issue #818: share_with_group() functionality
- Security checks for all resource types (files, groups, etc.)
- Permission level validation
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with ReBAC enabled and permissions enforced."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=True,
    )

    # Grant admin ownership of root directory for tests
    admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}
    nx.rebac_create(
        subject=("user", "admin"),
        relation="direct_owner",
        object=("file", "/"),
        context=admin_context,
    )

    yield nx
    nx.close()


@pytest.fixture
def admin_context() -> dict:
    """Create an admin operation context."""
    return {"user": "admin", "groups": [], "is_admin": True, "is_system": False}


class TestIssue817ShareWithUserSecurity:
    """Test Issue #817: Security checks in share_with_user()."""

    def test_owner_can_share_file(self, nx: NexusFS, temp_dir: Path, admin_context: dict) -> None:
        """Test that file owner can share with another user."""
        # Create a file as admin
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        tuple_id = nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            context=admin_context,
        )
        assert tuple_id

        # Admin should be able to share the file
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="alice",
            relation="viewer",
            context=admin_context,
        )
        assert share_id

        # Verify alice can now read the file
        assert nx.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", test_file),
        )

    def test_non_owner_cannot_share_file(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test that non-owner (viewer) cannot share file."""
        # Create a file as admin
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            context=admin_context,
        )

        # Grant alice viewer permission
        nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", test_file),
            context=admin_context,
        )

        # Alice (viewer) should NOT be able to share the file
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
        }
        with pytest.raises(PermissionError, match="does not have EXECUTE permission"):
            nx.share_with_user(
                resource=("file", test_file),
                user_id="bob",
                relation="viewer",
                context=alice_context,
            )

    def test_admin_can_always_share(self, nx: NexusFS, temp_dir: Path, admin_context: dict) -> None:
        """Test that admin users can share any file."""
        # Create a file as regular user
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Admin can share without ownership
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="alice",
            relation="viewer",
            context=admin_context,
        )
        assert share_id

    def test_system_context_can_always_share(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test that system context can share any file."""
        # Create a file
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # System context can share without ownership
        system_context = {
            "user": "system",
            "groups": [],
            "is_admin": False,
            "is_system": True,
        }
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="alice",
            relation="viewer",
            context=system_context,
        )
        assert share_id


class TestIssue818ShareWithGroup:
    """Test Issue #818: share_with_group() functionality."""

    def test_share_with_group_basic(self, nx: NexusFS, temp_dir: Path, admin_context: dict) -> None:
        """Test basic share_with_group functionality."""
        # Create a file as admin
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            context=admin_context,
        )

        # Create group membership
        nx.rebac_create(
            subject=("user", "alice"),
            relation="member",
            object=("group", "developers"),
            context=admin_context,
        )
        nx.rebac_create(
            subject=("user", "bob"),
            relation="member",
            object=("group", "developers"),
            context=admin_context,
        )

        # Share file with group
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="developers",
            relation="viewer",
            context=admin_context,
        )
        assert share_id

        # Verify both group members can read the file
        assert nx.rebac_check(
            subject=("user", "alice"),
            permission="read",
            object=("file", test_file),
        )
        assert nx.rebac_check(
            subject=("user", "bob"),
            permission="read",
            object=("file", test_file),
        )

    def test_non_owner_cannot_share_with_group(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test that non-owner cannot share with group."""
        # Create a file as admin
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            context=admin_context,
        )

        # Grant alice viewer permission
        nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", test_file),
            context=admin_context,
        )

        # Alice (viewer) should NOT be able to share with group
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
        }
        with pytest.raises(PermissionError, match="does not have EXECUTE permission"):
            nx.share_with_group(
                resource=("file", test_file),
                group_id="developers",
                relation="viewer",
                context=alice_context,
            )

    def test_share_with_group_permission_levels(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test different permission levels when sharing with group."""
        # Create a file as admin
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            context=admin_context,
        )

        # Create group membership
        nx.rebac_create(
            subject=("user", "alice"),
            relation="member",
            object=("group", "editors"),
            context=admin_context,
        )

        # Share file with group as editor
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="editors",
            relation="editor",
            context=admin_context,
        )
        assert share_id

        # Verify alice has write permission
        assert nx.rebac_check(
            subject=("user", "alice"),
            permission="write",
            object=("file", test_file),
        )


class TestNonFileResourceSecurity:
    """Test security checks for non-file resources (groups, workspaces, etc.)."""

    def test_owner_can_share_group(self, nx: NexusFS) -> None:
        """Test that group owner can grant permissions on the group."""
        # Create group ownership
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}
        nx.rebac_create(
            subject=("user", "admin"),
            relation="owner-of",
            object=("group", "developers"),
            context=admin_context,
        )

        # Admin should be able to grant permissions on the group
        tuple_id = nx.rebac_create(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("group", "developers"),
            context=admin_context,
        )
        assert tuple_id

    def test_non_owner_cannot_manage_group(self, nx: NexusFS) -> None:
        """Test that non-owner cannot grant permissions on group."""
        # Create group ownership for admin
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}
        nx.rebac_create(
            subject=("user", "admin"),
            relation="owner-of",
            object=("group", "developers"),
            context=admin_context,
        )

        # Grant alice viewer permission on the group
        nx.rebac_create(
            subject=("user", "alice"),
            relation="viewer-of",
            object=("group", "developers"),
            context=admin_context,
        )

        # Alice (viewer) should NOT be able to grant permissions
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
        }
        with pytest.raises(PermissionError, match="does not have owner permission"):
            nx.rebac_create(
                subject=("user", "bob"),
                relation="viewer-of",
                object=("group", "developers"),
                context=alice_context,
            )

    def test_workspace_permission_management(self, nx: NexusFS) -> None:
        """Test permission management for workspace resources."""
        # Create workspace ownership
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}
        nx.rebac_create(
            subject=("user", "admin"),
            relation="owner-of",
            object=("workspace", "/workspace1"),
            context=admin_context,
        )

        # Admin should be able to grant permissions on workspace
        tuple_id = nx.rebac_create(
            subject=("user", "alice"),
            relation="editor-of",
            object=("workspace", "/workspace1"),
            context=admin_context,
        )
        assert tuple_id

        # Non-owner should not be able to manage workspace permissions
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
        }
        with pytest.raises(PermissionError, match="does not have owner permission"):
            nx.rebac_create(
                subject=("user", "bob"),
                relation="viewer-of",
                object=("workspace", "/workspace1"),
                context=alice_context,
            )


class TestHelperMethodIntegration:
    """Test the _check_share_permission helper method integration."""

    def test_helper_used_in_rebac_create(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test that helper is properly integrated in rebac_create."""
        # Create a file
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            context=admin_context,
        )

        # Non-owner should be blocked by helper
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
        }
        with pytest.raises(PermissionError):
            nx.rebac_create(
                subject=("user", "bob"),
                relation="direct_viewer",
                object=("file", test_file),
                context=alice_context,
            )

    def test_no_context_allows_operation(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test that operations without context are allowed (backward compatibility)."""
        # Create a file
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Operation without context should succeed
        tuple_id = nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", test_file),
            context=None,
        )
        assert tuple_id

    def test_enforce_permissions_false_allows_all(self, temp_dir: Path) -> None:
        """Test that enforce_permissions=False bypasses checks."""
        nx = NexusFS(
            backend=LocalBackend(temp_dir),
            db_path=temp_dir / "metadata.db",
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            # Create a file
            test_file = "/test_file.txt"
            nx.write(test_file, b"test content")

            # Non-owner can share when permissions are not enforced
            alice_context = {
                "user": "alice",
                "groups": [],
                "is_admin": False,
                "is_system": False,
            }
            share_id = nx.share_with_user(
                resource=("file", test_file),
                user_id="bob",
                relation="viewer",
                context=alice_context,
            )
            assert share_id
        finally:
            nx.close()


class TestCrossTenantSharing:
    """Test cross-tenant sharing functionality."""

    def test_share_with_user_cross_tenant(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test sharing file with user in different tenant."""
        # Create a file as admin in tenant1
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        admin_context_tenant1 = {
            "user": "admin",
            "groups": [],
            "is_admin": True,
            "is_system": False,
            "tenant_id": "tenant1",
        }
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            tenant_id="tenant1",
            context=admin_context_tenant1,
        )

        # Share with user in tenant2
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="alice",
            relation="viewer",
            tenant_id="tenant1",
            user_tenant_id="tenant2",
            context=admin_context_tenant1,
        )
        assert share_id

    def test_share_with_group_cross_tenant(
        self, nx: NexusFS, temp_dir: Path, admin_context: dict
    ) -> None:
        """Test sharing file with group in different tenant."""
        # Create a file as admin in tenant1
        test_file = "/test_file.txt"
        nx.write(test_file, b"test content", context=OperationContext(**admin_context))

        # Grant admin ownership
        admin_context_tenant1 = {
            "user": "admin",
            "groups": [],
            "is_admin": True,
            "is_system": False,
            "tenant_id": "tenant1",
        }
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", test_file),
            tenant_id="tenant1",
            context=admin_context_tenant1,
        )

        # Share with group in tenant2
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="partner-team",
            relation="viewer",
            tenant_id="tenant1",
            group_tenant_id="tenant2",
            context=admin_context_tenant1,
        )
        assert share_id
