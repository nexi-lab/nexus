"""Integration tests for tenant admin sharing functionality (#819).

Tests that tenant admins can share resources within their tenant.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.server.auth.user_helpers import add_user_to_tenant


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


class TestTenantAdminSharing:
    """Test that tenant admins can share resources within their tenant."""

    def test_tenant_admin_can_share_file(self, nx: NexusFS) -> None:
        """Test that tenant admin can share files in their tenant."""
        # Setup: Create tenant structure
        tenant_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create tenant directory
        tenant_path = f"/tenant:{tenant_id}"
        nx.mkdir(tenant_path, context=OperationContext(**admin_context))

        # Create a file owned by a regular user (bob)
        file_path = f"{tenant_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            tenant_id=tenant_id,
            context=admin_context,
        )

        # Add alice as tenant admin
        add_user_to_tenant(nx._rebac_manager, "alice", tenant_id, role="admin")

        # Alice (tenant admin) should be able to share bob's file
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "tenant_id": tenant_id,
        }

        share_id = nx.share_with_user(
            resource=("file", file_path),
            user_id="charlie",
            relation="viewer",
            context=alice_context,
        )

        assert share_id
        # Verify charlie can now read the file
        assert nx.rebac_check(
            subject=("user", "charlie"),
            permission="read",
            object=("file", file_path),
        )

    def test_tenant_owner_can_share_file(self, nx: NexusFS) -> None:
        """Test that tenant owner can share files (owners are also admins)."""
        # Setup
        tenant_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create tenant directory
        tenant_path = f"/tenant:{tenant_id}"
        nx.mkdir(tenant_path, context=OperationContext(**admin_context))

        # Create a file
        file_path = f"{tenant_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership of file
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            context=admin_context,
        )

        # Add alice as tenant owner
        add_user_to_tenant(nx._rebac_manager, "alice", tenant_id, role="owner")

        # Alice (tenant owner) should be able to share bob's file
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "tenant_id": tenant_id,
        }

        share_id = nx.share_with_user(
            resource=("file", file_path),
            user_id="charlie",
            relation="viewer",
            context=alice_context,
        )

        assert share_id

    def test_tenant_admin_cannot_share_in_other_tenant(self, nx: NexusFS) -> None:
        """Test that tenant admin cannot share files in other tenants."""
        # Setup: Create two tenants
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create tenant1
        tenant1_path = "/tenant:acme"
        nx.mkdir(tenant1_path, context=OperationContext(**admin_context))
        file1_path = f"{tenant1_path}/doc.txt"
        nx.write(file1_path, b"test", context=OperationContext(**admin_context))
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file1_path),
            context=admin_context,
        )

        # Create tenant2
        tenant2_path = "/tenant:techcorp"
        nx.mkdir(tenant2_path, context=OperationContext(**admin_context))
        file2_path = f"{tenant2_path}/doc.txt"
        nx.write(file2_path, b"test", context=OperationContext(**admin_context))
        nx.rebac_create(
            subject=("user", "dave"),
            relation="direct_owner",
            object=("file", file2_path),
            context=admin_context,
        )

        # Add alice as admin of tenant1 only
        add_user_to_tenant(nx._rebac_manager, "alice", "acme", role="admin")

        # Alice should NOT be able to share files in tenant2
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "tenant_id": "acme",  # Alice is admin of acme, not techcorp
        }

        with pytest.raises(PermissionError, match="Only owners or tenant admins can share"):
            nx.share_with_user(
                resource=("file", file2_path),  # File in techcorp
                user_id="charlie",
                relation="viewer",
                context=alice_context,
            )

    def test_regular_member_cannot_share(self, nx: NexusFS) -> None:
        """Test that regular tenant member cannot share files they don't own."""
        # Setup
        tenant_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create tenant directory and file
        tenant_path = f"/tenant:{tenant_id}"
        nx.mkdir(tenant_path, context=OperationContext(**admin_context))
        file_path = f"{tenant_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            tenant_id=tenant_id,
            context=admin_context,
        )

        # Add alice as regular member (not admin)
        add_user_to_tenant(nx._rebac_manager, "alice", tenant_id, role="member")

        # Alice (regular member) should NOT be able to share bob's file
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "tenant_id": tenant_id,
        }

        with pytest.raises(PermissionError, match="Only owners or tenant admins can share"):
            nx.share_with_user(
                resource=("file", file_path),
                user_id="charlie",
                relation="viewer",
                context=alice_context,
            )

    def test_tenant_admin_can_share_with_group(self, nx: NexusFS) -> None:
        """Test that tenant admin can share files with groups."""
        # Setup
        tenant_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create tenant directory and file
        tenant_path = f"/tenant:{tenant_id}"
        nx.mkdir(tenant_path, context=OperationContext(**admin_context))
        file_path = f"{tenant_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            tenant_id=tenant_id,
            context=admin_context,
        )

        # Create group with members
        nx.rebac_create(
            subject=("user", "charlie"),
            relation="member",
            object=("group", "developers"),
            context=admin_context,
        )
        nx.rebac_create(
            subject=("user", "dave"),
            relation="member",
            object=("group", "developers"),
            context=admin_context,
        )

        # Add alice as tenant admin
        add_user_to_tenant(nx._rebac_manager, "alice", tenant_id, role="admin")

        # Alice (tenant admin) should be able to share with group
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "tenant_id": tenant_id,
        }

        share_id = nx.share_with_group(
            resource=("file", file_path),
            group_id="developers",
            relation="viewer",
            context=alice_context,
        )

        assert share_id
        # Verify group members can read the file
        assert nx.rebac_check(
            subject=("user", "charlie"),
            permission="read",
            object=("file", file_path),
        )
        assert nx.rebac_check(
            subject=("user", "dave"),
            permission="read",
            object=("file", file_path),
        )


class TestBackwardCompatibility:
    """Test that existing owner-based sharing still works."""

    def test_owner_can_still_share(self, nx: NexusFS) -> None:
        """Test that file owners can still share their files."""
        # Setup
        tenant_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create tenant directory and file
        tenant_path = f"/tenant:{tenant_id}"
        nx.mkdir(tenant_path, context=OperationContext(**admin_context))
        file_path = f"{tenant_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            tenant_id=tenant_id,
            context=admin_context,
        )

        # Bob (owner) should be able to share his own file
        bob_context = {
            "user": "bob",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "tenant_id": tenant_id,
        }

        share_id = nx.share_with_user(
            resource=("file", file_path),
            user_id="alice",
            relation="viewer",
            context=bob_context,
        )

        assert share_id
