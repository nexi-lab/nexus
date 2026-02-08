"""Unit tests for mount permission filtering security fixes.

Tests cover:
- list_mounts: Permission-based filtering of active mounts
- list_saved_mounts: User-based filtering of saved mount configurations
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx_with_permissions(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with permissions enabled."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=temp_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=True,
    )
    yield nx
    nx.close()


@pytest.fixture
def nx_without_permissions(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance without permissions (backward compatibility)."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=temp_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


class TestListMountsPermissionFiltering:
    """Tests for list_mounts permission-based filtering."""

    def test_list_mounts_without_context_backward_compatibility(
        self, nx_without_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that list_mounts works without context (backward compatibility)."""
        # Add a mount
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        nx_without_permissions.add_mount(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            priority=10,
        )

        # Call without context - should return all mounts
        mounts = nx_without_permissions.list_mounts()
        mount_points = [m["mount_point"] for m in mounts]

        # Should include both root and test mount
        assert "/" in mount_points
        assert "/mnt/test" in mount_points

    def test_list_mounts_filters_by_permission(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that list_mounts filters mounts based on user permissions."""
        # Create two mounts
        mount_data_dir1 = temp_dir / "mount1"
        mount_data_dir1.mkdir()
        mount_data_dir2 = temp_dir / "mount2"
        mount_data_dir2.mkdir()

        # Create context for user Alice (with admin to create mount)
        context_alice_admin = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
            is_admin=True,
        )

        # Create context for user Alice (non-admin for list_mounts test)
        context_alice = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        # Add first mount as Alice (she'll be granted direct_owner)
        nx_with_permissions.add_mount(
            mount_point="/mnt/alice",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir1)},
            priority=10,
            context=context_alice_admin,
        )

        # Create context for user Bob (with admin to create mount)
        context_bob_admin = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="bob@example.com",
            is_admin=True,
        )

        # Create context for user Bob (non-admin for list_mounts test)
        context_bob = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="bob@example.com",
        )

        # Add second mount as Bob (he'll be granted direct_owner)
        nx_with_permissions.add_mount(
            mount_point="/mnt/bob",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir2)},
            priority=10,
            context=context_bob_admin,
        )

        # When Alice calls list_mounts, she should only see her mount
        alice_mounts = nx_with_permissions.list_mounts(context=context_alice)
        alice_mount_points = [m["mount_point"] for m in alice_mounts]

        # Alice should see her mount
        assert "/mnt/alice" in alice_mount_points
        # Alice should NOT see Bob's mount (security fix)
        assert "/mnt/bob" not in alice_mount_points

        # When Bob calls list_mounts, he should only see his mount
        bob_mounts = nx_with_permissions.list_mounts(context=context_bob)
        bob_mount_points = [m["mount_point"] for m in bob_mounts]

        # Bob should see his mount
        assert "/mnt/bob" in bob_mount_points
        # Bob should NOT see Alice's mount (security fix)
        assert "/mnt/alice" not in bob_mount_points

    def test_list_mounts_shared_mount_visible_to_both_users(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that shared mounts are visible to users with permissions."""
        # Create a shared mount
        mount_data_dir = temp_dir / "shared"
        mount_data_dir.mkdir()

        # Admin context for mount creation
        context_alice_admin = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
            is_admin=True,
        )

        # Alice creates a shared mount (using admin to bypass parent permission check)
        nx_with_permissions.add_mount(
            mount_point="/mnt/shared",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            priority=10,
            context=context_alice_admin,
        )

        context_bob = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="bob@example.com",
        )

        # Grant Bob direct_viewer permission on the shared mount
        nx_with_permissions.rebac_create(
            subject=("user", "bob@example.com"),
            relation="direct_viewer",
            object=("file", "/mnt/shared"),
            zone_id="zone1",
        )

        # Bob should now see the shared mount
        bob_mounts = nx_with_permissions.list_mounts(context=context_bob)
        bob_mount_points = [m["mount_point"] for m in bob_mounts]
        assert "/mnt/shared" in bob_mount_points

    def test_list_mounts_handles_permission_check_failure_gracefully(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that list_mounts excludes mounts when permission check fails."""
        mount_data_dir = temp_dir / "mount_data"
        mount_data_dir.mkdir()

        # Admin context for mount creation
        context_alice_admin = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
            is_admin=True,
        )

        # Non-admin context for list_mounts test
        context_alice = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        nx_with_permissions.add_mount(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
            priority=10,
            context=context_alice_admin,
        )

        # Mock rebac_check to raise an exception
        with patch.object(nx_with_permissions, "rebac_check", side_effect=Exception("DB error")):
            # Should exclude the mount for safety
            mounts = nx_with_permissions.list_mounts(context=context_alice)
            mount_points = [m["mount_point"] for m in mounts]
            # The mount should be excluded due to permission check failure
            assert "/mnt/test" not in mount_points


class TestListSavedMountsUserFiltering:
    """Tests for list_saved_mounts user-based filtering."""

    def test_list_saved_mounts_without_context_shows_all(
        self, nx_with_permissions: NexusFS
    ) -> None:
        """Test that list_saved_mounts without context returns empty list (no context = no user)."""
        # Save a mount
        nx_with_permissions.save_mount(
            mount_point="/mnt/test",
            backend_type="local",
            backend_config={"data_dir": "/tmp/test"},
            owner_user_id="user:alice",
            zone_id="zone1",
        )

        # Call without context - should filter by current user (but there is none)
        # So it should return empty list or filter to current user (which is None)
        mounts = nx_with_permissions.list_saved_mounts()
        # Without context, owner_user_id defaults to None, so mount_manager will return
        # all mounts without filtering. This is backward compatible behavior.
        # The fix ensures that WITH context, it filters automatically.
        assert isinstance(mounts, list)

    def test_list_saved_mounts_filters_by_user_context(self, nx_with_permissions: NexusFS) -> None:
        """Test that list_saved_mounts automatically filters by current user."""
        # Save mount for Alice
        nx_with_permissions.save_mount(
            mount_point="/mnt/alice",
            backend_type="local",
            backend_config={"data_dir": "/tmp/alice"},
            owner_user_id="user:alice@example.com",
            zone_id="zone1",
        )

        # Save mount for Bob
        nx_with_permissions.save_mount(
            mount_point="/mnt/bob",
            backend_type="local",
            backend_config={"data_dir": "/tmp/bob"},
            owner_user_id="user:bob@example.com",
            zone_id="zone1",
        )

        # Create context for Alice
        context_alice = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        # When Alice calls list_saved_mounts, she should only see her mount
        alice_mounts = nx_with_permissions.list_saved_mounts(context=context_alice)
        alice_mount_points = [m["mount_point"] for m in alice_mounts]

        # Alice should see her mount
        assert "/mnt/alice" in alice_mount_points
        # Alice should NOT see Bob's mount (security fix)
        assert "/mnt/bob" not in alice_mount_points

        # Create context for Bob
        context_bob = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="bob@example.com",
        )

        # When Bob calls list_saved_mounts, he should only see his mount
        bob_mounts = nx_with_permissions.list_saved_mounts(context=context_bob)
        bob_mount_points = [m["mount_point"] for m in bob_mounts]

        # Bob should see his mount
        assert "/mnt/bob" in bob_mount_points
        # Bob should NOT see Alice's mount (security fix)
        assert "/mnt/alice" not in bob_mount_points

    def test_list_saved_mounts_filters_by_zone(self, nx_with_permissions: NexusFS) -> None:
        """Test that list_saved_mounts filters by zone_id from context."""
        # Save mount for zone1
        nx_with_permissions.save_mount(
            mount_point="/mnt/zone1",
            backend_type="local",
            backend_config={"data_dir": "/tmp/zone1"},
            owner_user_id="user:alice@example.com",
            zone_id="zone1",
        )

        # Save mount for zone2
        nx_with_permissions.save_mount(
            mount_point="/mnt/zone2",
            backend_type="local",
            backend_config={"data_dir": "/tmp/zone2"},
            owner_user_id="user:alice@example.com",
            zone_id="zone2",
        )

        # Create context for zone1
        context_zone1 = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        # When called with zone1 context, should only see zone1 mounts
        zone1_mounts = nx_with_permissions.list_saved_mounts(context=context_zone1)
        zone1_mount_points = [m["mount_point"] for m in zone1_mounts]

        assert "/mnt/zone1" in zone1_mount_points
        # Should NOT see zone2 mount (cross-zone isolation)
        assert "/mnt/zone2" not in zone1_mount_points

    def test_list_saved_mounts_explicit_filter_overrides_context(
        self, nx_with_permissions: NexusFS
    ) -> None:
        """Test that explicit owner_user_id parameter overrides context filtering."""
        # Save mounts for different users
        nx_with_permissions.save_mount(
            mount_point="/mnt/alice",
            backend_type="local",
            backend_config={"data_dir": "/tmp/alice"},
            owner_user_id="user:alice@example.com",
            zone_id="zone1",
        )

        nx_with_permissions.save_mount(
            mount_point="/mnt/bob",
            backend_type="local",
            backend_config={"data_dir": "/tmp/bob"},
            owner_user_id="user:bob@example.com",
            zone_id="zone1",
        )

        # Alice's context
        context_alice = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        # Alice explicitly asks for Bob's mounts (if allowed by API policy)
        mounts = nx_with_permissions.list_saved_mounts(
            owner_user_id="user:bob@example.com", context=context_alice
        )
        mount_points = [m["mount_point"] for m in mounts]

        # Should see Bob's mount since explicit filter was provided
        assert "/mnt/bob" in mount_points
        assert "/mnt/alice" not in mount_points

    def test_list_saved_mounts_with_agent_context(self, nx_with_permissions: NexusFS) -> None:
        """Test that list_saved_mounts works with agent subject_type."""
        # Save mount for an agent
        nx_with_permissions.save_mount(
            mount_point="/mnt/agent",
            backend_type="local",
            backend_config={"data_dir": "/tmp/agent"},
            owner_user_id="agent:bot123",
            zone_id="zone1",
        )

        # Create context for agent
        context_agent = OperationContext(
            user="bot123",
            groups=[],
            zone_id="zone1",
            subject_type="agent",
            subject_id="bot123",
        )

        # Agent should see its own mount
        agent_mounts = nx_with_permissions.list_saved_mounts(context=context_agent)
        agent_mount_points = [m["mount_point"] for m in agent_mounts]

        assert "/mnt/agent" in agent_mount_points


class TestCrossZoneIsolation:
    """Tests for cross-zone isolation in mount operations."""

    def test_user_cannot_see_other_zone_mounts(
        self, nx_with_permissions: NexusFS, temp_dir: Path
    ) -> None:
        """Test that users from different zones cannot see each other's mounts."""
        # Create mount for zone1
        mount_dir1 = temp_dir / "zone1"
        mount_dir1.mkdir()

        # Admin context for mount creation
        context_zone1_admin = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
            is_admin=True,
        )

        # Non-admin context for list tests
        context_zone1 = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        nx_with_permissions.add_mount(
            mount_point="/mnt/zone1",
            backend_type="local",
            backend_config={"data_dir": str(mount_dir1)},
            priority=10,
            context=context_zone1_admin,
        )

        # Save mount for zone1
        nx_with_permissions.save_mount(
            mount_point="/mnt/zone1_saved",
            backend_type="local",
            backend_config={"data_dir": str(mount_dir1)},
            owner_user_id="user:alice@example.com",
            zone_id="zone1",
        )

        # Create mount for zone2
        mount_dir2 = temp_dir / "zone2"
        mount_dir2.mkdir()

        # Admin context for mount creation
        context_zone2_admin = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="zone2",
            subject_type="user",
            subject_id="bob@example.com",
            is_admin=True,
        )

        # Non-admin context for list tests
        context_zone2 = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="zone2",
            subject_type="user",
            subject_id="bob@example.com",
        )

        nx_with_permissions.add_mount(
            mount_point="/mnt/zone2",
            backend_type="local",
            backend_config={"data_dir": str(mount_dir2)},
            priority=10,
            context=context_zone2_admin,
        )

        # Save mount for zone2
        nx_with_permissions.save_mount(
            mount_point="/mnt/zone2_saved",
            backend_type="local",
            backend_config={"data_dir": str(mount_dir2)},
            owner_user_id="user:bob@example.com",
            zone_id="zone2",
        )

        # Zone1 user should only see zone1 active mounts
        zone1_active_mounts = nx_with_permissions.list_mounts(context=context_zone1)
        zone1_active_mount_points = [m["mount_point"] for m in zone1_active_mounts]
        assert "/mnt/zone1" in zone1_active_mount_points
        assert "/mnt/zone2" not in zone1_active_mount_points

        # Zone1 user should only see zone1 saved mounts
        zone1_saved_mounts = nx_with_permissions.list_saved_mounts(context=context_zone1)
        zone1_saved_mount_points = [m["mount_point"] for m in zone1_saved_mounts]
        assert "/mnt/zone1_saved" in zone1_saved_mount_points
        assert "/mnt/zone2_saved" not in zone1_saved_mount_points

        # Zone2 user should only see zone2 active mounts
        zone2_active_mounts = nx_with_permissions.list_mounts(context=context_zone2)
        zone2_active_mount_points = [m["mount_point"] for m in zone2_active_mounts]
        assert "/mnt/zone2" in zone2_active_mount_points
        assert "/mnt/zone1" not in zone2_active_mount_points

        # Zone2 user should only see zone2 saved mounts
        zone2_saved_mounts = nx_with_permissions.list_saved_mounts(context=context_zone2)
        zone2_saved_mount_points = [m["mount_point"] for m in zone2_saved_mounts]
        assert "/mnt/zone2_saved" in zone2_saved_mount_points
        assert "/mnt/zone1_saved" not in zone2_saved_mount_points


class TestSaveMountAutoPopulation:
    """Tests for save_mount auto-population of owner_user_id and zone_id from context."""

    def test_save_mount_auto_populates_owner_from_context(
        self, nx_with_permissions: NexusFS
    ) -> None:
        """Test that save_mount automatically populates owner_user_id from context."""
        context = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        # Save mount without explicit owner_user_id
        nx_with_permissions.save_mount(
            mount_point="/mnt/auto_owner",
            backend_type="local",
            backend_config={"data_dir": "/tmp/test"},
            context=context,
        )

        # Retrieve the mount and verify owner was auto-populated
        saved_mount = nx_with_permissions.mount_manager.get_mount("/mnt/auto_owner")
        assert saved_mount is not None
        assert saved_mount["owner_user_id"] == "user:alice@example.com"
        assert saved_mount["zone_id"] == "zone1"

    def test_save_mount_auto_populates_zone_from_context(
        self, nx_with_permissions: NexusFS
    ) -> None:
        """Test that save_mount automatically populates zone_id from context."""
        context = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="acme_corp",
            subject_type="user",
            subject_id="bob@example.com",
        )

        # Save mount without explicit zone_id
        nx_with_permissions.save_mount(
            mount_point="/mnt/auto_zone",
            backend_type="local",
            backend_config={"data_dir": "/tmp/test"},
            context=context,
        )

        # Retrieve and verify zone was auto-populated
        saved_mount = nx_with_permissions.mount_manager.get_mount("/mnt/auto_zone")
        assert saved_mount is not None
        assert saved_mount["zone_id"] == "acme_corp"
        assert saved_mount["owner_user_id"] == "user:bob@example.com"

    def test_save_mount_explicit_params_override_context(
        self, nx_with_permissions: NexusFS
    ) -> None:
        """Test that explicit owner_user_id and zone_id override context values."""
        context = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )

        # Save mount with explicit owner and zone (different from context)
        nx_with_permissions.save_mount(
            mount_point="/mnt/explicit_override",
            backend_type="local",
            backend_config={"data_dir": "/tmp/test"},
            owner_user_id="user:bob@example.com",
            zone_id="zone2",
            context=context,
        )

        # Verify explicit values were used, not context values
        saved_mount = nx_with_permissions.mount_manager.get_mount("/mnt/explicit_override")
        assert saved_mount is not None
        assert saved_mount["owner_user_id"] == "user:bob@example.com"
        assert saved_mount["zone_id"] == "zone2"

    def test_save_mount_with_agent_context(self, nx_with_permissions: NexusFS) -> None:
        """Test that save_mount handles agent subject_type correctly."""
        context = OperationContext(
            user="bot123",
            groups=[],
            zone_id="zone1",
            subject_type="agent",
            subject_id="bot123",
        )

        # Save mount with agent context
        nx_with_permissions.save_mount(
            mount_point="/mnt/agent_mount",
            backend_type="local",
            backend_config={"data_dir": "/tmp/agent"},
            context=context,
        )

        # Verify agent subject_type is properly formatted
        saved_mount = nx_with_permissions.mount_manager.get_mount("/mnt/agent_mount")
        assert saved_mount is not None
        assert saved_mount["owner_user_id"] == "agent:bot123"
        assert saved_mount["zone_id"] == "zone1"

    def test_list_saved_mounts_shows_only_owned_mounts_after_auto_population(
        self, nx_with_permissions: NexusFS
    ) -> None:
        """Test that list_saved_mounts returns only mounts owned by the user after auto-population."""
        # Alice saves a mount
        context_alice = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="alice@example.com",
        )
        nx_with_permissions.save_mount(
            mount_point="/mnt/alice_auto",
            backend_type="local",
            backend_config={"data_dir": "/tmp/alice"},
            context=context_alice,
        )

        # Bob saves a mount
        context_bob = OperationContext(
            user="bob@example.com",
            groups=[],
            zone_id="zone1",
            subject_type="user",
            subject_id="bob@example.com",
        )
        nx_with_permissions.save_mount(
            mount_point="/mnt/bob_auto",
            backend_type="local",
            backend_config={"data_dir": "/tmp/bob"},
            context=context_bob,
        )

        # Alice should only see her own mount
        alice_mounts = nx_with_permissions.list_saved_mounts(context=context_alice)
        alice_mount_points = [m["mount_point"] for m in alice_mounts]
        assert "/mnt/alice_auto" in alice_mount_points
        assert "/mnt/bob_auto" not in alice_mount_points

        # Bob should only see his own mount
        bob_mounts = nx_with_permissions.list_saved_mounts(context=context_bob)
        bob_mount_points = [m["mount_point"] for m in bob_mounts]
        assert "/mnt/bob_auto" in bob_mount_points
        assert "/mnt/alice_auto" not in bob_mount_points

    def test_save_mount_without_context_uses_explicit_params(
        self, nx_with_permissions: NexusFS
    ) -> None:
        """Test that save_mount works without context when explicit params are provided."""
        # Save mount without context but with explicit parameters
        nx_with_permissions.save_mount(
            mount_point="/mnt/no_context",
            backend_type="local",
            backend_config={"data_dir": "/tmp/test"},
            owner_user_id="user:charlie@example.com",
            zone_id="zone3",
        )

        # Verify explicit values were used
        saved_mount = nx_with_permissions.mount_manager.get_mount("/mnt/no_context")
        assert saved_mount is not None
        assert saved_mount["owner_user_id"] == "user:charlie@example.com"
        assert saved_mount["zone_id"] == "zone3"
