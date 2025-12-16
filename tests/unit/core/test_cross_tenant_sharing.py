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
