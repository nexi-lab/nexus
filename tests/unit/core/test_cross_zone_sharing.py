"""Unit tests for Cross-Zone Sharing feature.

Tests cover:
- share_with_user API (same and cross-zone)
- revoke_share API
- list_incoming_shares and list_outgoing_shares
- Permission checks with cross-zone shares
- Tuple fetching includes cross-zone shares
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from nexus.core.rebac import CROSS_ZONE_ALLOWED_RELATIONS
from nexus.services.permissions.rebac_manager_zone_aware import (
    ZoneAwareReBACManager,
    ZoneIsolationError,
)
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def zone_aware_manager(engine):
    """Create a zone-aware ReBAC manager for testing.

    Uses cache_ttl_seconds=0 to disable caching for predictable test behavior.
    """
    manager = ZoneAwareReBACManager(
        engine=engine,
        cache_ttl_seconds=0,  # Disable cache for predictable tests
        max_depth=10,
        enforce_zone_isolation=True,
    )
    yield manager
    manager.close()


class TestCrossZoneAllowedRelations:
    """Tests for CROSS_ZONE_ALLOWED_RELATIONS configuration."""

    def test_shared_viewer_is_cross_zone_allowed(self):
        """Verify shared-viewer relation is in the allowed list."""
        assert "shared-viewer" in CROSS_ZONE_ALLOWED_RELATIONS

    def test_shared_editor_is_cross_zone_allowed(self):
        """Verify shared-editor relation is in the allowed list."""
        assert "shared-editor" in CROSS_ZONE_ALLOWED_RELATIONS

    def test_shared_owner_is_cross_zone_allowed(self):
        """Verify shared-owner relation is in the allowed list."""
        assert "shared-owner" in CROSS_ZONE_ALLOWED_RELATIONS

    def test_regular_relations_not_cross_zone_allowed(self):
        """Verify regular relations are NOT in the allowed list."""
        assert "viewer" not in CROSS_ZONE_ALLOWED_RELATIONS
        assert "editor" not in CROSS_ZONE_ALLOWED_RELATIONS
        assert "owner" not in CROSS_ZONE_ALLOWED_RELATIONS
        assert "member-of" not in CROSS_ZONE_ALLOWED_RELATIONS


class TestCrossZoneSharingWrite:
    """Tests for creating cross-zone shares."""

    def test_shared_viewer_allows_cross_zone(self, zone_aware_manager):
        """Test that shared-viewer relation allows cross-zone relationships."""
        # This should succeed - shared-viewer is allowed to cross zones
        tuple_id = zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",  # Different zone!
            object_zone_id="acme-zone",
        )
        assert tuple_id is not None

    def test_shared_editor_allows_cross_zone(self, zone_aware_manager):
        """Test that shared-editor relation allows cross-zone relationships."""
        tuple_id = zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )
        assert tuple_id is not None

    def test_regular_relation_blocks_cross_zone(self, zone_aware_manager):
        """Test that regular relations still block cross-zone."""
        # This should fail - viewer is NOT allowed to cross zones
        with pytest.raises(ZoneIsolationError, match="Cannot create cross-zone"):
            zone_aware_manager.rebac_write(
                subject=("user", "bob@partner.com"),
                relation="viewer",  # NOT in CROSS_ZONE_ALLOWED_RELATIONS
                object=("file", "/project/doc.txt"),
                zone_id="acme-zone",
                subject_zone_id="partner-zone",
                object_zone_id="acme-zone",
            )

    def test_same_zone_shared_viewer_allowed(self, zone_aware_manager):
        """Test that shared-viewer also works for same-zone sharing."""
        # Same-zone sharing should work too
        tuple_id = zone_aware_manager.rebac_write(
            subject=("user", "alice@acme.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="acme-zone",
            object_zone_id="acme-zone",
        )
        assert tuple_id is not None

    def test_cross_zone_share_stored_with_object_zone(self, zone_aware_manager):
        """Test that cross-zone shares are stored with object's zone_id."""
        # Create cross-zone share
        tuple_id = zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )
        assert tuple_id is not None  # Verify share was created

        # Verify tuple exists by checking permission
        result = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is True


class TestCrossZoneSharingPermissionCheck:
    """Tests for permission checks with cross-zone shares."""

    def test_cross_zone_user_can_check_shared_resource(self, zone_aware_manager):
        """Test that cross-zone user can check permission on shared resource."""
        # Create cross-zone share
        zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # Check permission - bob should have shared-viewer on the file
        result = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is True

    def test_unshared_cross_zone_user_denied(self, zone_aware_manager):
        """Test that cross-zone users without shares are denied."""
        # No share created for charlie
        result = zone_aware_manager.rebac_check(
            subject=("user", "charlie@other.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is False


class TestCrossZoneMultipleShares:
    """Tests for multiple cross-zone shares."""

    def test_multiple_cross_zone_shares(self, zone_aware_manager):
        """Test creating shares to multiple cross-zone users."""
        # Create shares from two different zones
        tuple_id_1 = zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/acme/doc1.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )
        tuple_id_2 = zone_aware_manager.rebac_write(
            subject=("user", "charlie@other.com"),
            relation="shared-editor",
            object=("file", "/acme/doc2.txt"),
            zone_id="acme-zone",
            subject_zone_id="other-zone",
            object_zone_id="acme-zone",
        )

        # Both shares should exist
        assert tuple_id_1 is not None
        assert tuple_id_2 is not None

        # Both users should have access
        result_bob = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/acme/doc1.txt"),
            zone_id="acme-zone",
        )
        result_charlie = zone_aware_manager.rebac_check(
            subject=("user", "charlie@other.com"),
            permission="shared-editor",
            object=("file", "/acme/doc2.txt"),
            zone_id="acme-zone",
        )
        assert result_bob is True
        assert result_charlie is True

    def test_user_with_multiple_shares_from_different_zones(self, zone_aware_manager):
        """Test that a user can receive shares from multiple zones."""
        # Bob receives shares from two different zones
        zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )
        zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/xyz/doc.txt"),
            zone_id="xyz-zone",
            subject_zone_id="partner-zone",
            object_zone_id="xyz-zone",
        )

        # Bob should have access to both resources (checking each in its own zone)
        result_acme = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
        )
        result_xyz = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/xyz/doc.txt"),
            zone_id="xyz-zone",
        )
        assert result_acme is True
        assert result_xyz is True


class TestCrossZoneSharingRevoke:
    """Tests for revoking cross-zone shares."""

    def test_revoke_cross_zone_share(self, zone_aware_manager):
        """Test revoking a cross-zone share."""
        # Create cross-zone share
        tuple_id = zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # Verify share exists
        result = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is True

        # Revoke share
        deleted = zone_aware_manager.rebac_delete(tuple_id)
        assert deleted is True

        # Verify share is gone
        result = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is False


class TestCrossZoneSharingWithExpiration:
    """Tests for cross-zone shares with expiration."""

    def test_expired_cross_zone_share_denied(self, zone_aware_manager):
        """Test that expired cross-zone shares are denied."""
        # Create share that expires in the past
        zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
            expires_at=datetime.now(UTC) - timedelta(hours=1),  # Already expired
        )

        # Permission check should fail (expired)
        result = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is False

    def test_non_expired_cross_zone_share_allowed(self, zone_aware_manager):
        """Test that non-expired cross-zone shares are allowed."""
        # Create share that expires in the future
        zone_aware_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
            expires_at=datetime.now(UTC) + timedelta(days=7),  # Expires in a week
        )

        # Permission check should succeed
        result = zone_aware_manager.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="shared-viewer",
            object=("file", "/project/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is True


class TestCrossZoneRustPathFix:
    """Tests for cross-zone sharing in Rust acceleration path.

    These tests verify that cross-zone shares work when using the Rust
    path for permission checks. The key fix is in _fetch_tuples_for_rust()
    which now includes cross-zone tuples for the subject.
    """

    @pytest.fixture
    def enhanced_manager(self, engine):
        """Create an enhanced ReBAC manager that has _fetch_tuples_for_rust."""
        from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

        manager = EnhancedReBACManager(
            engine=engine,
            cache_ttl_seconds=0,
            max_depth=10,
            enforce_zone_isolation=True,
        )
        yield manager
        manager.close()

    def test_fetch_tuples_for_rust_includes_cross_zone(self, enhanced_manager):
        """Test that _fetch_tuples_for_rust includes cross-zone tuples."""
        from nexus.core.rebac import Entity

        # Create cross-zone share: partner-zone user gets access to acme-zone file
        enhanced_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # Fetch tuples from partner-zone (Bob's home zone) WITH subject
        subject = Entity("user", "bob@partner.com")
        tuples = enhanced_manager._fetch_tuples_for_rust(zone_id="partner-zone", subject=subject)

        # Should include the cross-zone share even though it's stored in acme-zone
        cross_zone_tuples = [
            t
            for t in tuples
            if t["relation"] == "shared-editor" and t["subject_id"] == "bob@partner.com"
        ]
        assert len(cross_zone_tuples) == 1
        assert cross_zone_tuples[0]["object_id"] == "/acme/doc.txt"

    def test_fetch_tuples_for_rust_without_subject_excludes_cross_zone(self, enhanced_manager):
        """Test that _fetch_tuples_for_rust without subject excludes cross-zone."""
        # Create cross-zone share
        enhanced_manager.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # Fetch tuples from partner-zone WITHOUT subject (backward compatibility)
        tuples = enhanced_manager._fetch_tuples_for_rust(zone_id="partner-zone")

        # Should NOT include cross-zone share (stored in acme-zone)
        cross_zone_tuples = [
            t
            for t in tuples
            if t["relation"] == "shared-editor" and t["subject_id"] == "bob@partner.com"
        ]
        assert len(cross_zone_tuples) == 0


class TestCrossZonePermissionExpansion:
    """Tests for permission expansion with cross-zone shares.

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

        manager = ZoneAwareReBACManager(
            engine=engine,
            cache_ttl_seconds=0,
            max_depth=10,
            enforce_zone_isolation=True,
        )
        manager.create_namespace(DEFAULT_FILE_NAMESPACE)
        yield manager
        manager.close()

    def test_shared_editor_grants_read_permission(self, manager_with_namespace):
        """Test that shared-editor grants read permission via namespace union."""
        # Create cross-zone share with shared-editor
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # Check read permission - should be granted via:
        # read -> viewer -> shared-editor
        result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="read",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is True

    def test_shared_editor_grants_write_permission(self, manager_with_namespace):
        """Test that shared-editor grants write permission via namespace union."""
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-editor",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # Check write permission - should be granted via:
        # write -> editor -> shared-editor
        result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="write",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
        )
        assert result is True

    def test_shared_viewer_grants_only_read_permission(self, manager_with_namespace):
        """Test that shared-viewer grants read but NOT write permission."""
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-viewer",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # Read should be granted
        read_result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="read",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
        )
        assert read_result is True

        # Write should NOT be granted (shared-viewer only in viewer union, not editor)
        write_result = manager_with_namespace.rebac_check(
            subject=("user", "bob@partner.com"),
            permission="write",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
        )
        assert write_result is False

    def test_shared_owner_grants_all_permissions(self, manager_with_namespace):
        """Test that shared-owner grants read, write, and owner permissions."""
        manager_with_namespace.rebac_write(
            subject=("user", "bob@partner.com"),
            relation="shared-owner",
            object=("file", "/acme/doc.txt"),
            zone_id="acme-zone",
            subject_zone_id="partner-zone",
            object_zone_id="acme-zone",
        )

        # All permissions should be granted
        for permission in ["read", "write", "owner"]:
            result = manager_with_namespace.rebac_check(
                subject=("user", "bob@partner.com"),
                permission=permission,
                object=("file", "/acme/doc.txt"),
                zone_id="acme-zone",
            )
            assert result is True, f"Expected {permission} to be granted"
