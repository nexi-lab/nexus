"""Unit tests for zone boundary security (Issue #819).

Tests that admins with admin:read:* cannot access files from other zones
unless they have MANAGE_ZONES capability (system admin only).
"""

import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest

pytest.importorskip("pyroaring")


from nexus import CASLocalBackend, NexusFS
from nexus.bricks.rebac.permissions_enhanced import AdminCapability
from nexus.contracts.types import OperationContext
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.lib.zone_helpers import add_user_to_zone
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
async def nx(temp_dir: Path) -> AsyncGenerator[NexusFS, None]:
    """Create a NexusFS instance with permissions enforced."""
    nx = create_nexus_fs(
        backend=CASLocalBackend(temp_dir),
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(
            enforce=True, allow_admin_bypass=True
        ),  # Enable admin bypass for zone boundary tests
    )
    # Grant system user access to /nexus/pipes (required by RecordStoreWriteObserver
    # when permissions are enforced)
    system_context = {"user_id": "system", "groups": [], "is_admin": False, "is_system": True}
    nx.service("rebac").rebac_create_sync(
        subject=("user", "system"),
        relation="direct_owner",
        object=("file", "/nexus"),
        context=system_context,
    )
    yield nx
    nx.close()


class TestZoneBoundarySecurity:
    """Test zone boundary security for admin bypass."""

    @pytest.mark.asyncio
    async def test_zone_admin_cannot_access_other_zone_files(self, nx: NexusFS) -> None:
        """Test that zone admin cannot access files from other zones."""
        # Setup: Create file in zone1 as system admin with MANAGE_ZONES
        system_admin = OperationContext(
            user_id="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id=None,  # No zone restriction - can access all zones
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
                AdminCapability.MANAGE_ZONES,
            },
        )

        # Create zone directories
        nx.mkdir("/zone", context=system_admin)
        nx.mkdir("/zone/acme", context=system_admin)

        test_file = "/zone/acme/doc.txt"
        nx.write(test_file, b"secret acme data", context=system_admin)

        # Zone admin from zone2 (techcorp) tries to access zone1 (acme) file
        zone_admin_techcorp = OperationContext(
            user_id="alice",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id="techcorp",  # Alice is admin of techcorp, not acme
            admin_capabilities={
                AdminCapability.READ_ALL,  # Has wildcard read, but NOT MANAGE_ZONES
                AdminCapability.WRITE_ALL,
            },
        )

        # Should be denied - cross-zone access without MANAGE_ZONES
        with pytest.raises(PermissionError, match="Access denied"):
            nx.sys_read(test_file, context=zone_admin_techcorp)

    @pytest.mark.asyncio
    async def test_system_admin_can_access_any_zone(self, nx: NexusFS) -> None:
        """Test that system admin with MANAGE_ZONES can access any zone."""
        # Setup: Create file in zone1
        system_admin_setup = OperationContext(
            user_id="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id=None,
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
                AdminCapability.MANAGE_ZONES,
            },
        )

        # Create zone directories
        nx.mkdir("/zone", context=system_admin_setup)
        nx.mkdir("/zone/acme", context=system_admin_setup)

        test_file = "/zone/acme/doc.txt"
        nx.write(test_file, b"secret acme data", context=system_admin_setup)

        # System admin from zone2 should be able to access zone1 file
        system_admin_zone2 = OperationContext(
            user_id="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id="techcorp",  # Different zone
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.MANAGE_ZONES,  # System admin capability
            },
        )

        # Should succeed - system admin with MANAGE_ZONES
        content = nx.sys_read(test_file, context=system_admin_zone2)
        assert content == b"secret acme data"

    @pytest.mark.asyncio
    async def test_zone_admin_can_access_own_zone(self, nx: NexusFS) -> None:
        """Test that zone admin can access files in their own zone."""
        # Setup: Create file in zone1
        system_admin = OperationContext(
            user_id="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id=None,
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
                AdminCapability.MANAGE_ZONES,
            },
        )

        # Create zone directories
        nx.mkdir("/zone", context=system_admin)
        nx.mkdir("/zone/acme", context=system_admin)

        test_file = "/zone/acme/doc.txt"
        nx.write(test_file, b"acme data", context=system_admin)

        # Add alice as zone admin for acme
        add_user_to_zone(nx.service("rebac")._rebac_manager, "alice", "acme", role="admin")

        # Zone admin from same zone should be able to access
        zone_admin_acme = OperationContext(
            user_id="alice",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id="acme",  # Same zone
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
            },
        )

        # Should succeed - same zone
        content = nx.sys_read(test_file, context=zone_admin_acme)
        assert content == b"acme data"

    @pytest.mark.asyncio
    async def test_cross_zone_write_denied(self, nx: NexusFS) -> None:
        """Test that zone admin cannot write to other zone's files."""
        # Setup: Create file in zone1
        system_admin = OperationContext(
            user_id="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id=None,
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
                AdminCapability.MANAGE_ZONES,
            },
        )

        # Create zone directories
        nx.mkdir("/zone", context=system_admin)
        nx.mkdir("/zone/acme", context=system_admin)

        test_file = "/zone/acme/doc.txt"
        nx.write(test_file, b"original", context=system_admin)

        # Zone admin from zone2 tries to write to zone1 file
        zone_admin_techcorp = OperationContext(
            user_id="alice",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id="techcorp",  # Different zone
            admin_capabilities={
                AdminCapability.WRITE_ALL,  # Has wildcard write, but NOT MANAGE_ZONES
            },
        )

        # Should be denied - cross-zone write without MANAGE_ZONES
        with pytest.raises(PermissionError, match="Access denied"):
            nx.write(test_file, b"hacked!", context=zone_admin_techcorp)

    @pytest.mark.asyncio
    async def test_system_admin_without_manage_zones_denied(self, nx: NexusFS) -> None:
        """Test that admin without MANAGE_ZONES cannot access other zones."""
        # Setup: Create file in zone1
        system_admin = OperationContext(
            user_id="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id=None,
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
                AdminCapability.MANAGE_ZONES,
            },
        )

        # Create zone directories
        nx.mkdir("/zone", context=system_admin)
        nx.mkdir("/zone/acme", context=system_admin)

        test_file = "/zone/acme/doc.txt"
        nx.write(test_file, b"secret", context=system_admin)

        # Admin without MANAGE_ZONES tries to access different zone
        limited_admin = OperationContext(
            user_id="limited_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            zone_id="techcorp",  # Different zone
            admin_capabilities={
                AdminCapability.READ_ALL,  # Has wildcard read
                # But NOT MANAGE_ZONES
            },
        )

        # Should be denied
        with pytest.raises(PermissionError, match="Access denied"):
            nx.sys_read(test_file, context=limited_admin)
