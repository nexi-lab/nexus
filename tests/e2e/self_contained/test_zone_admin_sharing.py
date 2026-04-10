"""Integration tests for zone admin sharing functionality (#819).

Tests that zone admins can share resources within their zone.
Uses rebac_service sync API after __getattr__ retirement (PR #2774).
"""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.contracts.types import OperationContext
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.lib.zone_helpers import add_user_to_zone
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
async def nx(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with ReBAC enabled and permissions enforced."""
    rebac_db = temp_dir / "rebac.db"
    monkeypatch.setenv("NEXUS_DATABASE_URL", f"sqlite:///{rebac_db}")
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    nx = await create_nexus_fs(
        backend=CASLocalBackend(temp_dir),
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=True),
    )

    # Grant admin ownership of root directory for tests
    admin_context = {"user_id": "admin", "groups": [], "is_admin": True, "is_system": False}
    nx.service("rebac").rebac_create_sync(
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

    @pytest.mark.asyncio
    async def test_zone_admin_can_share_file(self, nx: NexusFS) -> None:
        """Test that zone admin can share files in their zone."""
        # Setup: Create zone structure
        zone_id = "acme"
        admin_context = {"user_id": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory (using Windows-compatible path)
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))

        # Create a file owned by a regular user (bob)
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.service("rebac").rebac_create_sync(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
            context=admin_context,
        )

        # Add alice as zone admin
        add_user_to_zone(nx.service("rebac_manager"), "alice", zone_id, role="admin")

        # Alice (zone admin) should be able to share bob's file
        alice_context = {
            "user_id": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        share_id = nx.service("rebac").share_with_user_sync(
            resource=("file", file_path),
            user_id="charlie",
            relation="viewer",
            context=alice_context,
        )

        assert share_id
        # Verify charlie can now read the file
        assert nx.service("rebac").rebac_check_sync(
            subject=("user", "charlie"),
            permission="read",
            object=("file", file_path),
        )

    @pytest.mark.asyncio
    async def test_zone_owner_can_share_file(self, nx: NexusFS) -> None:
        """Test that zone owner can share files (owners are also admins)."""
        # Setup
        zone_id = "acme"
        admin_context = {"user_id": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))

        # Create a file
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership of file
        nx.service("rebac").rebac_create_sync(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            context=admin_context,
        )

        # Add alice as zone owner
        add_user_to_zone(nx.service("rebac_manager"), "alice", zone_id, role="owner")

        # Alice (zone owner) should be able to share bob's file
        alice_context = {
            "user_id": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        share_id = nx.service("rebac").share_with_user_sync(
            resource=("file", file_path),
            user_id="charlie",
            relation="viewer",
            context=alice_context,
        )

        assert share_id

    @pytest.mark.asyncio
    async def test_zone_admin_cannot_share_in_other_zone(self, nx: NexusFS) -> None:
        """Test that zone admin cannot share files in other zones."""
        # Setup: Create two zones
        admin_context = {"user_id": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone1
        zone1_path = "/zone/acme"
        nx.mkdir(zone1_path, context=OperationContext(**admin_context))
        file1_path = f"{zone1_path}/doc.txt"
        nx.write(file1_path, b"test", context=OperationContext(**admin_context))
        nx.service("rebac").rebac_create_sync(
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
        nx.service("rebac").rebac_create_sync(
            subject=("user", "dave"),
            relation="direct_owner",
            object=("file", file2_path),
            context=admin_context,
        )

        # Add alice as admin of zone1 only
        add_user_to_zone(nx.service("rebac_manager"), "alice", "acme", role="admin")

        # Alice should NOT be able to share files in zone2
        alice_context = {
            "user_id": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": "acme",  # Alice is admin of acme, not techcorp
        }

        with pytest.raises(PermissionError, match="Only owners or zone admins can share"):
            nx.service("rebac").share_with_user_sync(
                resource=("file", file2_path),  # File in techcorp
                user_id="charlie",
                relation="viewer",
                context=alice_context,
            )

    @pytest.mark.asyncio
    async def test_regular_member_cannot_share(self, nx: NexusFS) -> None:
        """Test that regular zone member cannot share files they don't own."""
        # Setup
        zone_id = "acme"
        admin_context = {"user_id": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory and file
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.service("rebac").rebac_create_sync(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
            context=admin_context,
        )

        # Add alice as regular member (not admin)
        add_user_to_zone(nx.service("rebac_manager"), "alice", zone_id, role="member")

        # Alice (regular member) should NOT be able to share bob's file
        alice_context = {
            "user_id": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        with pytest.raises(PermissionError, match="Only owners or zone admins can share"):
            nx.service("rebac").share_with_user_sync(
                resource=("file", file_path),
                user_id="charlie",
                relation="viewer",
                context=alice_context,
            )

    @pytest.mark.asyncio
    async def test_zone_admin_can_share_with_group(self, nx: NexusFS) -> None:
        """Test that zone admin can share files with groups."""
        # Setup
        zone_id = "acme"
        admin_context = {"user_id": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory and file
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.service("rebac").rebac_create_sync(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
            context=admin_context,
        )

        # Create group with members
        nx.service("rebac").rebac_create_sync(
            subject=("user", "charlie"),
            relation="member",
            object=("group", "developers"),
            context=admin_context,
        )
        nx.service("rebac").rebac_create_sync(
            subject=("user", "dave"),
            relation="member",
            object=("group", "developers"),
            context=admin_context,
        )

        # Add alice as zone admin
        add_user_to_zone(nx.service("rebac_manager"), "alice", zone_id, role="admin")

        # Alice (zone admin) should be able to share with group
        alice_context = {
            "user_id": "alice",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        share_id = nx.service("rebac").share_with_group_sync(
            resource=("file", file_path),
            group_id="developers",
            relation="viewer",
            context=alice_context,
        )

        assert share_id
        # Verify group members can read the file
        assert nx.service("rebac").rebac_check_sync(
            subject=("user", "charlie"),
            permission="read",
            object=("file", file_path),
        )
        assert nx.service("rebac").rebac_check_sync(
            subject=("user", "dave"),
            permission="read",
            object=("file", file_path),
        )


class TestBackwardCompatibility:
    """Test that existing owner-based sharing still works."""

    @pytest.mark.asyncio
    async def test_owner_can_still_share(self, nx: NexusFS) -> None:
        """Test that file owners can still share their files."""
        # Setup
        zone_id = "acme"
        admin_context = {"user_id": "admin", "groups": [], "is_admin": True, "is_system": False}

        # Create zone directory and file
        zone_path = f"/zone/{zone_id}"
        nx.mkdir(zone_path, context=OperationContext(**admin_context))
        file_path = f"{zone_path}/doc.txt"
        nx.write(file_path, b"test content", context=OperationContext(**admin_context))

        # Grant bob ownership
        nx.service("rebac").rebac_create_sync(
            subject=("user", "bob"),
            relation="direct_owner",
            object=("file", file_path),
            zone_id=zone_id,
            context=admin_context,
        )

        # Bob (owner) should be able to share his own file
        bob_context = {
            "user_id": "bob",
            "groups": [],
            "is_admin": False,
            "is_system": False,
            "zone_id": zone_id,
        }

        share_id = nx.service("rebac").share_with_user_sync(
            resource=("file", file_path),
            user_id="alice",
            relation="viewer",
            context=bob_context,
        )

        assert share_id
