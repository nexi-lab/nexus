"""Tests for NexusFederation orchestrator (Issue #2808, Decision 9A).

Pull model architecture:
- share() is purely local (create zone + DT_MOUNT)
- join() discovers DT_MOUNT via VFS, joins Raft group, mounts locally

Covers:
- share() success path (local only)
- share() with explicit zone_id
- join() success path (discover + join + mount)
- join() discovery failures (path not found, not a mount, no zone_id)
- join() membership failure
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.raft.federation import NexusFederation

# All async tests use anyio marker with asyncio backend only (trio not installed)
pytestmark = [pytest.mark.anyio, pytest.mark.parametrize("anyio_backend", ["asyncio"])]


def _make_zone_manager(
    node_id: int = 1,
    root_zone_id: str = "root",
    advertise_addr: str = "localhost:2126",
) -> MagicMock:
    """Create a mock ZoneManager."""
    mgr = MagicMock()
    mgr.node_id = node_id
    mgr.root_zone_id = root_zone_id
    mgr.advertise_addr = advertise_addr
    mgr.tls_config = None
    mgr.share_subtree.return_value = "zone-abc123"
    return mgr


# ---------------------------------------------------------------------------
# share() tests — purely local (pull model)
# ---------------------------------------------------------------------------


class TestFederationShare:
    async def test_share_success(self) -> None:
        """share() creates zone via share_subtree, returns zone_id."""
        mgr = _make_zone_manager()

        fed = NexusFederation(zone_manager=mgr)
        zone_id = await fed.share("/usr/alice/project")

        assert zone_id == "zone-abc123"
        mgr.share_subtree.assert_called_once_with(
            parent_zone_id="root",
            path="/usr/alice/project",
            zone_id=None,
        )

    async def test_share_with_explicit_zone_id(self) -> None:
        """share() passes explicit zone_id to share_subtree."""
        mgr = _make_zone_manager()
        mgr.share_subtree.return_value = "my-custom-zone"

        fed = NexusFederation(zone_manager=mgr)
        zone_id = await fed.share("/data", zone_id="my-custom-zone")

        assert zone_id == "my-custom-zone"
        mgr.share_subtree.assert_called_once_with(
            parent_zone_id="root",
            path="/data",
            zone_id="my-custom-zone",
        )

    async def test_share_uses_root_zone_constant_when_none(self) -> None:
        """share() falls back to ROOT_ZONE_ID constant when root_zone_id is None."""
        mgr = _make_zone_manager()
        mgr.root_zone_id = None

        fed = NexusFederation(zone_manager=mgr)
        await fed.share("/data")

        # Should use ROOT_ZONE_ID constant from contracts
        mgr.share_subtree.assert_called_once()
        call_kwargs = mgr.share_subtree.call_args[1]
        assert call_kwargs["parent_zone_id"] is not None


# ---------------------------------------------------------------------------
# join() tests — discover + join + mount (pull model)
# ---------------------------------------------------------------------------


class TestFederationJoin:
    async def test_join_success(self) -> None:
        """join() discovers DT_MOUNT, joins zone, mounts locally."""
        mgr = _make_zone_manager(node_id=3)

        fed = NexusFederation(zone_manager=mgr)

        # Mock _discover_mount to return DT_MOUNT metadata
        fed._discover_mount = AsyncMock(
            return_value={
                "is_mount": True,
                "entry_type": 2,  # DT_MOUNT
                "target_zone_id": "zone-xyz789",
            }
        )
        # Mock _request_membership
        fed._request_membership = AsyncMock()

        zone_id = await fed.join("bob:2126", "/shared-project", "/usr/charlie/shared")

        assert zone_id == "zone-xyz789"

        # Step 1: Discovered zone via _discover_mount
        fed._discover_mount.assert_awaited_once_with("bob:2126", "/shared-project")

        # Step 2: Joined zone locally
        mgr.join_zone.assert_called_once_with("zone-xyz789", peers=["bob:2126"])

        # Step 3: Requested membership via JoinZone RPC
        fed._request_membership.assert_awaited_once_with(
            peer_addr="bob:2126",
            zone_id="zone-xyz789",
            node_id=3,
            node_address="localhost:2126",
        )

        # Step 4: Mounted
        mgr.mount.assert_called_once_with("root", "/usr/charlie/shared", "zone-xyz789")

    async def test_join_path_not_found(self) -> None:
        """join() raises ValueError if remote path doesn't exist."""
        mgr = _make_zone_manager()

        fed = NexusFederation(zone_manager=mgr)
        fed._discover_mount = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="not found on peer"):
            await fed.join("bob:2126", "/nonexistent", "/local")

    async def test_join_not_a_mount(self) -> None:
        """join() raises ValueError if remote path is not a DT_MOUNT."""
        mgr = _make_zone_manager()

        fed = NexusFederation(zone_manager=mgr)
        fed._discover_mount = AsyncMock(
            return_value={
                "is_mount": False,
                "entry_type": 1,  # DT_DIR
            }
        )

        with pytest.raises(ValueError, match="not a.*DT_MOUNT"):
            await fed.join("bob:2126", "/regular-dir", "/local")

    async def test_join_no_zone_id_in_mount(self) -> None:
        """join() raises ValueError if DT_MOUNT has no target zone_id."""
        mgr = _make_zone_manager()

        fed = NexusFederation(zone_manager=mgr)
        fed._discover_mount = AsyncMock(
            return_value={
                "is_mount": True,
                "entry_type": 2,
                "target_zone_id": None,
            }
        )

        with pytest.raises(ValueError, match="no target zone_id"):
            await fed.join("bob:2126", "/mount-without-zone", "/local")

    async def test_join_empty_zone_id_in_mount(self) -> None:
        """join() raises ValueError if DT_MOUNT has empty target zone_id."""
        mgr = _make_zone_manager()

        fed = NexusFederation(zone_manager=mgr)
        fed._discover_mount = AsyncMock(
            return_value={
                "is_mount": True,
                "entry_type": 2,
                "target_zone_id": "",
            }
        )

        with pytest.raises(ValueError, match="no target zone_id"):
            await fed.join("bob:2126", "/mount-empty-zone", "/local")

    async def test_join_membership_failure(self) -> None:
        """join() propagates _request_membership errors."""
        mgr = _make_zone_manager(node_id=2)

        fed = NexusFederation(zone_manager=mgr)
        fed._discover_mount = AsyncMock(
            return_value={
                "is_mount": True,
                "entry_type": 2,
                "target_zone_id": "zone-fail",
            }
        )
        fed._request_membership = AsyncMock(
            side_effect=RuntimeError("Cannot join zone 'zone-fail'")
        )

        with pytest.raises(RuntimeError, match="Cannot join zone"):
            await fed.join("bob:2126", "/shared", "/local")

    async def test_join_uses_root_zone_constant_when_none(self) -> None:
        """join() falls back to ROOT_ZONE_ID constant when root_zone_id is None."""
        mgr = _make_zone_manager(node_id=2)
        mgr.root_zone_id = None

        fed = NexusFederation(zone_manager=mgr)
        fed._discover_mount = AsyncMock(
            return_value={
                "is_mount": True,
                "entry_type": 2,
                "target_zone_id": "zone-test",
            }
        )
        fed._request_membership = AsyncMock()

        await fed.join("peer:2126", "/shared", "/local")

        # mount() should be called with the ROOT_ZONE_ID constant
        mgr.mount.assert_called_once()
        call_args = mgr.mount.call_args[0]
        assert call_args[0] is not None  # parent_zone_id should not be None
