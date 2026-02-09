"""Integration tests for zone admin sharing functionality (#819).

Tests that zone admins can share resources within their zone.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.server.auth.user_helpers import add_user_to_zone
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with ReBAC enabled and permissions enforced."""
    # Isolate ReBAC database per test to prevent cross-test state pollution
    # The global _engine cache in database.py must be reset for each test
    import nexus.storage.database as db_module

    db_module._engine = None

    rebac_db = temp_dir / "rebac.db"
    monkeypatch.setenv("NEXUS_DATABASE_URL", f"sqlite:///{rebac_db}")
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    nx = create_nexus_fs(
        backend=LocalBackend(temp_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=temp_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
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

    # Create /zone directory for zone-based paths
    nx.mkdir("/zone", context=OperationContext(**admin_context))

    yield nx
    nx.close()


class TestZoneAdminSharing:
    """Test that zone admins can share resources within their zone."""

    def test_zone_admin_can_share_file(self, nx: NexusFS) -> None:
        """Test that zone admin can share files in their zone."""
        # Setup: Create zone structure
        zone_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory (using Windows-compatible path)
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))

        # Create a file owned by a regular user (bob)
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
            context=admin_context,
        )

        # Add alice as zone admin
        add_user_to_zone(nx._rebac_manager, "alice", zone_id, role="admin")

        # Alice (zone admin) should be able to share bob's file
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
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

    def test_zone_owner_can_share_file(self, nx: NexusFS) -> None:
        """Test that zone owner can share files (owners are also admins)."""
        # Setup
        zone_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))

        # Create a file
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership of file
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            context=admin_context,
        )

        # Add alice as zone owner
        add_user_to_zone(nx._rebac_manager, "alice", zone_id, role="owner")

        # Alice (zone owner) should be able to share bob's file
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        share_id = nx.share_with_user(
            resource=("file", file_path),
            user_id="charlie",
            relation="viewer",
            context=alice_context,
        )

        assert share_id

    def test_zone_admin_cannot_share_in_other_zone(self, nx: NexusFS) -> None:
        """Test that zone admin cannot share files in other zones."""
        # Setup: Create two zones
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone1
        zone1_path = "/zone/acme"
        nx.mkdir(zone1_path, context=OperationContext(**admin_context))
        file1_path = f"{zone1_path}/doc.txt"
        nx.write(file1_path, b"test", context=OperationContext(**admin_context))
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file1_path),
            context=admin_context,
        )

        # Create zone2
        zone2_path = "/zone/techcorp"
        nx.mkdir(zone2_path, context=OperationContext(**admin_context))
        file2_path = f"{zone2_path}/doc.txt"
        nx.write(file2_path, b"test", context=OperationContext(**admin_context))
        nx.rebac_create(
            subject=("user", "dave"),
            relation="direct_owner",
            object=("file", file2_path),
            context=admin_context,
        )

        # Add alice as admin of zone1 only
        add_user_to_zone(nx._rebac_manager, "alice", "acme", role="admin")

        # Alice should NOT be able to share files in zone2
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": "acme",  # Alice is admin of acme, not techcorp
        }

        with pytest.raises(PermissionError, match="Only owners or zone admins can share"):
            nx.share_with_user(
                resource=("file", file2_path),  # File in techcorp
                user_id="charlie",
                relation="viewer",
                context=alice_context,
            )

    def test_regular_member_cannot_share(self, nx: NexusFS) -> None:
        """Test that regular zone member cannot share files they don't own."""
        # Setup
        zone_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory and file
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
            context=admin_context,
        )

        # Add alice as regular member (not admin)
        add_user_to_zone(nx._rebac_manager, "alice", zone_id, role="member")

        # Alice (regular member) should NOT be able to share bob's file
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        with pytest.raises(PermissionError, match="Only owners or zone admins can share"):
            nx.share_with_user(
                resource=("file", file_path),
                user_id="charlie",
                relation="viewer",
                context=alice_context,
            )

    def test_zone_admin_can_share_with_group(self, nx: NexusFS) -> None:
        """Test that zone admin can share files with groups."""
        # Setup
        zone_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory and file
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
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

        # Add alice as zone admin
        add_user_to_zone(nx._rebac_manager, "alice", zone_id, role="admin")

        # Alice (zone admin) should be able to share with group
        alice_context = {
            "user": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
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
        zone_id = "acme"
        admin_context = {"user": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory and file
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.rebac_create(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
            context=admin_context,
        )

        # Bob (owner) should be able to share his own file
        bob_context = {
            "user": "bob",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        share_id = nx.share_with_user(
            resource=("file", file_path),
            user_id="alice",
            relation="viewer",
            context=bob_context,
        )

        assert share_id
