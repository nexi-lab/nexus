"""Tests for share_with_user() and share_with_group() permission checks.

Tests cover:
- Issue #817: Permission checks in share_with_user()
- Issue #818: share_with_group() functionality and permission checks
- Admin/system bypass behavior
- Cross-tenant sharing scenarios
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext, Permission


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with ReBAC enabled."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=True,  # Enable permissions for ReBAC tests
    )
    yield nx
    nx.close()


@pytest.fixture
def nx_no_permissions(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance without permissions enforcement."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


@pytest.fixture
def test_file(nx: NexusFS) -> str:
    """Create a test file and set up ownership for testing."""
    file_path = "/test/share_test.txt"
    
    # Create file as admin (bypasses permission checks)
    admin_ctx = OperationContext(
        user="admin",
        groups=[],
        is_admin=True,
        tenant_id="default",
    )
    nx.write(file_path, b"test content", context=admin_ctx)
    
    # Grant owner permission to alice (for execute permission tests)
    nx.rebac_create(
        subject=("user", "alice"),
        relation="direct_owner",
        object=("file", file_path),
        tenant_id="default",
        context=admin_ctx,
    )
    
    return file_path


@pytest.fixture
def owner_context() -> OperationContext:
    """OperationContext for user with execute permission (owner)."""
    return OperationContext(
        user="alice",
        groups=[],
        is_admin=False,
        is_system=False,
        tenant_id="default",
    )


@pytest.fixture
def viewer_context() -> OperationContext:
    """OperationContext for user with only read permission."""
    return OperationContext(
        user="bob",
        groups=[],
        is_admin=False,
        is_system=False,
        tenant_id="default",
    )


@pytest.fixture
def editor_context() -> OperationContext:
    """OperationContext for user with write permission but no execute."""
    return OperationContext(
        user="charlie",
        groups=[],
        is_admin=False,
        is_system=False,
        tenant_id="default",
    )


@pytest.fixture
def admin_context() -> OperationContext:
    """OperationContext for admin user."""
    return OperationContext(
        user="admin",
        groups=[],
        is_admin=True,
        is_system=False,
        tenant_id="default",
    )


@pytest.fixture
def system_context() -> OperationContext:
    """OperationContext for system operations."""
    return OperationContext(
        user="system",
        groups=[],
        is_admin=False,
        is_system=True,
        tenant_id="default",
    )


class TestShareWithUserPermissions:
    """Tests for share_with_user() permission enforcement (Issue #817)."""

    def test_owner_can_share_file(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test that user with execute permission (owner) can share file."""
        # Alice is owner, so she should be able to share
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="dave",
            relation="viewer",
            context=owner_context,
        )
        
        assert share_id is not None
        assert isinstance(share_id, str)
        
        # Verify the share was created
        result = nx._rebac_manager.rebac_check(
            subject=("user", "dave"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        assert result is True

    def test_admin_can_share_without_execute(
        self, nx: NexusFS, test_file: str, admin_context: OperationContext
    ) -> None:
        """Test that admin users can share without execute permission."""
        # Admin should bypass permission checks
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="dave",
            relation="viewer",
            context=admin_context,
        )
        
        assert share_id is not None

    def test_system_can_share_without_execute(
        self, nx: NexusFS, test_file: str, system_context: OperationContext
    ) -> None:
        """Test that system operations can share without execute permission."""
        # System should bypass permission checks
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="dave",
            relation="viewer",
            context=system_context,
        )
        
        assert share_id is not None

    def test_viewer_cannot_share_file(
        self, nx: NexusFS, test_file: str, viewer_context: OperationContext
    ) -> None:
        """Test that user with only read permission cannot share."""
        # Grant bob read permission but not execute
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        nx.rebac_create(
            subject=("user", "bob"),
            relation="viewer-of",
            object=("file", test_file),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Bob should not be able to share
        with pytest.raises(PermissionError, match="does not have EXECUTE"):
            nx.share_with_user(
                resource=("file", test_file),
                user_id="dave",
                relation="viewer",
                context=viewer_context,
            )

    def test_editor_cannot_share_file(
        self, nx: NexusFS, test_file: str, editor_context: OperationContext
    ) -> None:
        """Test that user with write permission but no execute cannot share."""
        # Grant charlie write permission but not execute
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        nx.rebac_create(
            subject=("user", "charlie"),
            relation="editor-of",
            object=("file", test_file),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Charlie should not be able to share
        with pytest.raises(PermissionError, match="does not have EXECUTE"):
            nx.share_with_user(
                resource=("file", test_file),
                user_id="dave",
                relation="viewer",
                context=editor_context,
            )

    def test_no_permission_cannot_share(
        self, nx: NexusFS, test_file: str
    ) -> None:
        """Test that user with no permission cannot share."""
        no_permission_ctx = OperationContext(
            user="eve",
            groups=[],
            is_admin=False,
            is_system=False,
            tenant_id="default",
        )
        
        # Eve has no permissions, should not be able to share
        with pytest.raises(PermissionError, match="does not have EXECUTE"):
            nx.share_with_user(
                resource=("file", test_file),
                user_id="dave",
                relation="viewer",
                context=no_permission_ctx,
            )

    def test_non_file_resource_no_permission_check(
        self, nx: NexusFS, owner_context: OperationContext
    ) -> None:
        """Test that sharing non-file resources doesn't require permission check."""
        # Non-file resources should not require execute permission check
        share_id = nx.share_with_user(
            resource=("group", "developers"),
            user_id="dave",
            relation="viewer",
            context=owner_context,
        )
        
        assert share_id is not None

    def test_permissions_disabled_allows_share(
        self, nx_no_permissions: NexusFS, viewer_context: OperationContext
    ) -> None:
        """Test that when permissions are disabled, anyone can share."""
        file_path = "/test/no_perms.txt"
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        nx_no_permissions.write(file_path, b"content", context=admin_ctx)
        
        # Even a viewer should be able to share when permissions are disabled
        share_id = nx_no_permissions.share_with_user(
            resource=("file", file_path),
            user_id="dave",
            relation="viewer",
            context=viewer_context,
        )
        
        assert share_id is not None

    def test_cross_tenant_share_permission_check(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test that permission check works for cross-tenant shares."""
        # Alice (owner) should be able to share cross-tenant
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="partner_user",
            relation="viewer",
            tenant_id="default",
            user_tenant_id="partner-tenant",
            context=owner_context,
        )
        
        assert share_id is not None
        
        # Verify cross-tenant share was created
        result = nx._rebac_manager.rebac_check(
            subject=("user", "partner_user"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        assert result is True

    def test_share_with_dict_context(
        self, nx: NexusFS, test_file: str
    ) -> None:
        """Test that share_with_user works with dict context."""
        # Dict context should be converted to OperationContext
        dict_context = {
            "user": "alice",
            "groups": [],
            "tenant_id": "default",
            "is_admin": False,
            "is_system": False,
        }
        
        share_id = nx.share_with_user(
            resource=("file", test_file),
            user_id="dave",
            relation="viewer",
            context=dict_context,
        )
        
        assert share_id is not None

    def test_share_with_none_context_no_permission_check(
        self, nx_no_permissions: NexusFS
    ) -> None:
        """Test that None context works when permissions are disabled."""
        file_path = "/test/none_context.txt"
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        nx_no_permissions.write(file_path, b"content", context=admin_ctx)
        
        # Should work when permissions are disabled
        share_id = nx_no_permissions.share_with_user(
            resource=("file", file_path),
            user_id="dave",
            relation="viewer",
            context=None,
        )
        
        assert share_id is not None

    def test_share_all_relation_types(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test that owner can share with all relation types."""
        for relation in ["viewer", "editor", "owner"]:
            share_id = nx.share_with_user(
                resource=("file", test_file),
                user_id=f"user_{relation}",
                relation=relation,
                context=owner_context,
            )
            
            assert share_id is not None
            
            # Verify correct relation was created
            expected_relation = f"shared-{relation}"
            result = nx._rebac_manager.rebac_check(
                subject=("user", f"user_{relation}"),
                permission=expected_relation,
                object=("file", test_file),
                tenant_id="default",
            )
            assert result is True


class TestShareWithGroup:
    """Tests for share_with_group() functionality (Issue #818)."""

    def test_share_file_with_group(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test basic group sharing functionality."""
        # First create a group with members
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        # Create group membership
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        nx.rebac_create(
            subject=("user", "member2"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Share file with group
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="developers",
            relation="viewer",
            context=owner_context,
        )
        
        assert share_id is not None
        
        # Verify group members have access via userset-as-subject pattern
        result1 = nx._rebac_manager.rebac_check(
            subject=("user", "member1"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        result2 = nx._rebac_manager.rebac_check(
            subject=("user", "member2"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        
        assert result1 is True
        assert result2 is True

    def test_share_with_group_uses_userset_pattern(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test that share_with_group uses userset-as-subject pattern."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        # Create group membership
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "test-group"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Share with group
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="test-group",
            relation="viewer",
            context=owner_context,
        )
        
        assert share_id is not None
        
        # Verify the tuple uses userset-as-subject pattern: ("group", "test-group", "member")
        tuples = nx._rebac_manager.rebac_list_tuples(
            subject=("group", "test-group", "member"),
            relation="shared-viewer",
            object=("file", test_file),
        )
        
        assert len(tuples) > 0
        assert tuples[0]["subject_type"] == "group"
        assert tuples[0]["subject_id"] == "test-group"
        assert tuples[0]["subject_relation"] == "member"

    def test_owner_can_share_with_group(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test that owner can share with group."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="developers",
            relation="viewer",
            context=owner_context,
        )
        
        assert share_id is not None

    def test_non_owner_cannot_share_with_group(
        self, nx: NexusFS, test_file: str, viewer_context: OperationContext
    ) -> None:
        """Test that non-owner cannot share with group."""
        # Grant bob read permission but not execute
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        nx.rebac_create(
            subject=("user", "bob"),
            relation="viewer-of",
            object=("file", test_file),
            tenant_id="default",
            context=admin_ctx,
        )
        
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Bob should not be able to share
        with pytest.raises(PermissionError, match="does not have EXECUTE"):
            nx.share_with_group(
                resource=("file", test_file),
                group_id="developers",
                relation="viewer",
                context=viewer_context,
            )

    def test_admin_can_share_with_group(
        self, nx: NexusFS, test_file: str, admin_context: OperationContext
    ) -> None:
        """Test that admin can share with group."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="developers",
            relation="viewer",
            context=admin_context,
        )
        
        assert share_id is not None

    def test_share_with_group_all_relation_types(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test sharing with group using all relation types."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        for relation in ["viewer", "editor", "owner"]:
            group_id = f"group_{relation}"
            nx.rebac_create(
                subject=("user", "member1"),
                relation="member-of",
                object=("group", group_id),
                tenant_id="default",
                context=admin_ctx,
            )
            
            share_id = nx.share_with_group(
                resource=("file", test_file),
                group_id=group_id,
                relation=relation,
                context=owner_context,
            )
            
            assert share_id is not None
            
            # Verify correct relation was created
            expected_relation = f"shared-{relation}"
            result = nx._rebac_manager.rebac_check(
                subject=("user", "member1"),
                permission=expected_relation,
                object=("file", test_file),
                tenant_id="default",
            )
            assert result is True

    def test_cross_tenant_group_sharing(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test cross-tenant group sharing."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        # Create group in partner tenant
        nx.rebac_create(
            subject=("user", "partner_member"),
            relation="member-of",
            object=("group", "partner-team"),
            tenant_id="partner-tenant",
            context=admin_ctx,
        )
        
        # Share cross-tenant
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="partner-team",
            relation="viewer",
            tenant_id="default",
            group_tenant_id="partner-tenant",
            context=owner_context,
        )
        
        assert share_id is not None
        
        # Verify cross-tenant share works
        result = nx._rebac_manager.rebac_check(
            subject=("user", "partner_member"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        assert result is True

    def test_share_with_group_expiration(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test sharing with group using expiration."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        expires_at = datetime.now(UTC) + timedelta(days=7)
        
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="developers",
            relation="viewer",
            expires_at=expires_at,
            context=owner_context,
        )
        
        assert share_id is not None
        
        # Verify member has access before expiration
        result = nx._rebac_manager.rebac_check(
            subject=("user", "member1"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        assert result is True

    def test_group_membership_changes_affect_access(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test that adding/removing group members affects access."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        # Create group and share
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        share_id = nx.share_with_group(
            resource=("file", test_file),
            group_id="developers",
            relation="viewer",
            context=owner_context,
        )
        
        assert share_id is not None
        
        # Verify member1 has access
        result1 = nx._rebac_manager.rebac_check(
            subject=("user", "member1"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        assert result1 is True
        
        # Add new member
        nx.rebac_create(
            subject=("user", "member2"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Verify new member also has access
        result2 = nx._rebac_manager.rebac_check(
            subject=("user", "member2"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        assert result2 is True

    def test_multiple_groups_on_same_resource(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test sharing same resource with multiple groups."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        # Create two groups
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        nx.rebac_create(
            subject=("user", "member2"),
            relation="member-of",
            object=("group", "designers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Share with both groups
        share1 = nx.share_with_group(
            resource=("file", test_file),
            group_id="developers",
            relation="viewer",
            context=owner_context,
        )
        share2 = nx.share_with_group(
            resource=("file", test_file),
            group_id="designers",
            relation="editor",
            context=owner_context,
        )
        
        assert share1 is not None
        assert share2 is not None
        
        # Verify both groups have access
        result1 = nx._rebac_manager.rebac_check(
            subject=("user", "member1"),
            permission="shared-viewer",
            object=("file", test_file),
            tenant_id="default",
        )
        result2 = nx._rebac_manager.rebac_check(
            subject=("user", "member2"),
            permission="shared-editor",
            object=("file", test_file),
            tenant_id="default",
        )
        
        assert result1 is True
        assert result2 is True

    def test_share_with_group_invalid_relation(
        self, nx: NexusFS, test_file: str, owner_context: OperationContext
    ) -> None:
        """Test that invalid relation raises ValueError."""
        with pytest.raises(ValueError, match="relation must be"):
            nx.share_with_group(
                resource=("file", test_file),
                group_id="developers",
                relation="invalid",
                context=owner_context,
            )

    def test_share_with_group_non_file_resource(
        self, nx: NexusFS, owner_context: OperationContext
    ) -> None:
        """Test that sharing non-file resources doesn't require permission check."""
        admin_ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            tenant_id="default",
        )
        
        nx.rebac_create(
            subject=("user", "member1"),
            relation="member-of",
            object=("group", "developers"),
            tenant_id="default",
            context=admin_ctx,
        )
        
        # Non-file resources should not require execute permission check
        share_id = nx.share_with_group(
            resource=("group", "other-group"),
            group_id="developers",
            relation="viewer",
            context=owner_context,
        )
        
        assert share_id is not None
