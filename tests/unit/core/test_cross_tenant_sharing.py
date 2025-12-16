"""Unit tests for Cross-Tenant Sharing feature.

Tests cover:
- share_with_user API (same and cross-tenant)
- revoke_share API
- list_incoming_shares and list_outgoing_shares
- Permission checks with cross-tenant shares
- Tuple fetching includes cross-tenant shares
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from nexus.core.rebac import CROSS_TENANT_ALLOWED_RELATIONS
from nexus.core.rebac_manager_tenant_aware import TenantAwareReBACManager, TenantIsolationError
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def tenant_aware_manager(engine):
    """Create a tenant-aware ReBAC manager for testing.

    Uses cache_ttl_seconds=0 to disable caching for predictable test behavior.
    """
    manager = TenantAwareReBACManager(
        engine=engine,
        cache_ttl_seconds=0,  # Disable cache for predictable tests
        max_depth=10,
        enforce_tenant_isolation=True,
    )
    yield manager
    manager.close()


class TestCrossTenantAllowedRelations:
    """Tests for CROSS_TENANT_ALLOWED_RELATIONS configuration."""

    def test_shared_viewer_is_cross_tenant_allowed(self):
        """Verify shared-viewer relation is in the allowed list."""
        assert "shared-viewer" in CROSS_TENANT_ALLOWED_RELATIONS

    def test_shared_editor_is_cross_tenant_allowed(self):
        """Verify shared-editor relation is in the allowed list."""
        assert "shared-editor" in CROSS_TENANT_ALLOWED_RELATIONS

    def test_shared_owner_is_cross_tenant_allowed(self):
        """Verify shared-owner relation is in the allowed list."""
        assert "shared-owner" in CROSS_TENANT_ALLOWED_RELATIONS

    def test_regular_relations_not_cross_tenant_allowed(self):
        """Verify regular relations are NOT in the allowed list."""
        assert "viewer" not in CROSS_TENANT_ALLOWED_RELATIONS
        assert "editor" not in CROSS_TENANT_ALLOWED_RELATIONS
        assert "owner" not in CROSS_TENANT_ALLOWED_RELATIONS
        assert "member-of" not in CROSS_TENANT_ALLOWED_RELATIONS


class TestCrossTenantSharingWrite:
    """Tests for creating cross-tenant shares."""

    def test_shared_viewer_allows_cross_tenant(self, tenant_aware_manager):
        """Test that shared-viewer relation allows cross-tenant relationships."""
        # This should succeed - shared-viewer is allowed to cross tenants
        tuple_id = tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",  # Different tenant!
            object_tenant_id="acme-tenant",
        )
        assert tuple_id is not None

    def test_shared_editor_allows_cross_tenant(self, tenant_aware_manager):
        """Test that shared-editor relation allows cross-tenant relationships."""
        tuple_id = tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )
        assert tuple_id is not None

    def test_regular_relation_blocks_cross_tenant(self, tenant_aware_manager):
        """Test that regular relations still block cross-tenant."""
        # This should fail - viewer is NOT allowed to cross tenants
        with pytest.raises(TenantIsolationError, match="Cannot create cross-tenant"):
            tenant_aware_manager.rebac_write(
                subject=("user", "bob@partner.com"),
                relation="viewer",  # NOT in CROSS_TENANT_ALLOWED_RELATIONS
                object=("file", "/project/doc.txt"),
                tenant_id="acme-tenant",
                subject_tenant_id="partner-tenant",
                object_tenant_id="acme-tenant",
            )

    def test_same_tenant_shared_viewer_allowed(self, tenant_aware_manager):
        """Test that shared-viewer also works for same-tenant sharing."""
        # Same-tenant sharing should work too
        tuple_id = tenant_aware_manager.rebac_write(
            subject=("user", "alice@acme.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="acme-tenant",
            object_tenant_id="acme-tenant",
        )
        assert tuple_id is not None

    def test_cross_tenant_share_stored_with_object_tenant(self, tenant_aware_manager):
        """Test that cross-tenant shares are stored with object's tenant_id."""
        # Create cross-tenant share
        tuple_id = tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )
        assert tuple_id is not None  # Verify share was created

        # Verify tuple exists by checking permission
        result = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is True


class TestCrossTenantSharingPermissionCheck:
    """Tests for permission checks with cross-tenant shares."""

    def test_cross_tenant_user_can_check_shared_resource(self, tenant_aware_manager):
        """Test that cross-tenant user can check permission on shared resource."""
        # Create cross-tenant share
        tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # Check permission - bob should have shared-viewer on the file
        result = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is True

    def test_unshared_cross_tenant_user_denied(self, tenant_aware_manager):
        """Test that cross-tenant users without shares are denied."""
        # No share created for charlie
        result = tenant_aware_manager.rebac_check(
            subject=("user", "charlie@other.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is False


class TestCrossTenantMultipleShares:
    """Tests for multiple cross-tenant shares."""

    def test_multiple_cross_tenant_shares(self, tenant_aware_manager):
        """Test creating shares to multiple cross-tenant users."""
        # Create shares from two different tenants
        tuple_id_1 = tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/acme/doc1.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )
        tuple_id_2 = tenant_aware_manager.rebac_write(
            subject=("user", "charlie@other.com"),
            relation="shared-editor",
            object=("file", "/acme/doc2.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="other-tenant",
            object_tenant_id="acme-tenant",
        )

        # Both shares should exist
        assert tuple_id_1 is not None
        assert tuple_id_2 is not None

        # Both users should have access
        result_bob = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/acme/doc1.txt"),
            tenant_id="acme-tenant",
        )
        result_charlie = tenant_aware_manager.rebac_check(
            subject=("user", "charlie@other.com"),
            permission="shared-editor",
            object=("file", "/acme/doc2.txt"),
            tenant_id="acme-tenant",
        )
        assert result_bob is True
        assert result_charlie is True

    def test_user_with_multiple_shares_from_different_tenants(self, tenant_aware_manager):
        """Test that a user can receive shares from multiple tenants."""
        # Bob receives shares from two different tenants
        tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )
        tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/xyz/doc.txt"),
            tenant_id="xyz-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="xyz-tenant",
        )

        # Bob should have access to both resources (checking each in its own tenant)
        result_acme = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
        )
        result_xyz = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/xyz/doc.txt"),
            tenant_id="xyz-tenant",
        )
        assert result_acme is True
        assert result_xyz is True


class TestCrossTenantSharingRevoke:
    """Tests for revoking cross-tenant shares."""

    def test_revoke_cross_tenant_share(self, tenant_aware_manager):
        """Test revoking a cross-tenant share."""
        # Create cross-tenant share
        tuple_id = tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # Verify share exists
        result = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is True

        # Revoke share
        deleted = tenant_aware_manager.rebac_delete(tuple_id)
        assert deleted is True

        # Verify share is gone
        result = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is False


class TestCrossTenantSharingWithExpiration:
    """Tests for cross-tenant shares with expiration."""

    def test_expired_cross_tenant_share_denied(self, tenant_aware_manager):
        """Test that expired cross-tenant shares are denied."""
        # Create share that expires in the past
        tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
            expires_at=datetime.now(UTC) - timedelta(hours=1),  # Already expired
        )

        # Permission check should fail (expired)
        result = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is False

    def test_non_expired_cross_tenant_share_allowed(self, tenant_aware_manager):
        """Test that non-expired cross-tenant shares are allowed."""
        # Create share that expires in the future
        tenant_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
            expires_at=datetime.now(UTC) + timedelta(days=7),  # Expires in a week
        )

        # Permission check should succeed
        result = tenant_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is True


class TestCrossTenantRustPathFix:
    """Tests for cross-tenant sharing in Rust acceleration path.

    These tests verify that cross-tenant shares work when using the Rust
    path for permission checks. The key fix is in _fetch_tuples_for_rust()
    which now includes cross-tenant tuples for the subject.
    """

    @pytest.fixture
    def enhanced_manager(self, engine):
        """Create an enhanced ReBAC manager that has _fetch_tuples_for_rust."""
        from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

        manager = EnhancedReBACManager(
            engine=engine,
            cache_ttl_seconds=0,
            max_depth=10,
            enforce_tenant_isolation=True,
        )
        yield manager
        manager.close()

    def test_fetch_tuples_for_rust_includes_cross_tenant(self, enhanced_manager):
        """Test that _fetch_tuples_for_rust includes cross-tenant tuples."""
        from nexus.core.rebac import Entity

        # Create cross-tenant share: partner-tenant user gets access to acme-tenant file
        enhanced_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # Fetch tuples from partner-tenant (Bob's home tenant) WITH subject
        subject = Entity("user", "bob@partner.com")
        tuples = enhanced_manager._fetch_tuples_for_rust(
            tenant_id="partner-tenant", subject=subject
        )

        # Should include the cross-tenant share even though it's stored in acme-tenant
        cross_tenant_tuples = [
            t
            for t in tuples
            if t["relation"] == "shared-editor" and t["subject_id"] == "bob@partner.com"
        ]
        assert len(cross_tenant_tuples) == 1
        assert cross_tenant_tuples[0]["object_id"] == "/acme/doc.txt"

    def test_fetch_tuples_for_rust_without_subject_excludes_cross_tenant(self, enhanced_manager):
        """Test that _fetch_tuples_for_rust without subject excludes cross-tenant."""
        # Create cross-tenant share
        enhanced_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # Fetch tuples from partner-tenant WITHOUT subject (backward compatibility)
        tuples = enhanced_manager._fetch_tuples_for_rust(tenant_id="partner-tenant")

        # Should NOT include cross-tenant share (stored in acme-tenant)
        cross_tenant_tuples = [
            t
            for t in tuples
            if t["relation"] == "shared-editor" and t["subject_id"] == "bob@partner.com"
        ]
        assert len(cross_tenant_tuples) == 0


class TestCrossTenantPermissionExpansion:
    """Tests for permission expansion with cross-tenant shares.

    These tests verify that shared-* relations properly grant permissions
    through the namespace union configuration:
    - shared-editor grants read and write permissions
    - shared-viewer grants read permission
    - shared-owner grants read, write, and owner permissions
    """

    @pytest.fixture
    def manager_with_namespace(self, engine):
        """Create manager with file namespace for permission expansion."""
        from nexus.core.rebac import DEFAULT_FILE_NAMESPACE

        manager = TenantAwareReBACManager(
            engine=engine,
            cache_ttl_seconds=0,
            max_depth=10,
            enforce_tenant_isolation=True,
        )
        manager.create_namespace(DEFAULT_FILE_NAMESPACE)
        yield manager
        manager.close()

    def test_shared_editor_grants_read_permission(self, manager_with_namespace):
        """Test that shared-editor grants read permission via namespace union."""
        # Create cross-tenant share with shared-editor
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # Check read permission - should be granted via:
        # read -> viewer -> shared-editor
        result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="read",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is True

    def test_shared_editor_grants_write_permission(self, manager_with_namespace):
        """Test that shared-editor grants write permission via namespace union."""
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # Check write permission - should be granted via:
        # write -> editor -> shared-editor
        result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="write",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert result is True

    def test_shared_viewer_grants_only_read_permission(self, manager_with_namespace):
        """Test that shared-viewer grants read but NOT write permission."""
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # Read should be granted
        read_result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="read",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert read_result is True

        # Write should NOT be granted (shared-viewer only in viewer union, not editor)
        write_result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="write",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
        )
        assert write_result is False

    def test_shared_owner_grants_all_permissions(self, manager_with_namespace):
        """Test that shared-owner grants read, write, and owner permissions."""
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-owner",
            object=("file", "/acme/doc.txt"),
            tenant_id="acme-tenant",
            subject_tenant_id="partner-tenant",
            object_tenant_id="acme-tenant",
        )

        # All permissions should be granted
        for permission in ["read", "write", "owner"]:
            result = manager_with_namespace.rebac_check(
                subject=("user", "bob@partner.com"),
                permission=permission,
                object=("file", "/acme/doc.txt"),
                tenant_id="acme-tenant",
            )
            assert result is True, f"Expected {permission} to be granted"
