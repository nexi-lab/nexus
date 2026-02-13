"""Tests for NexusFederation orchestration (Issue #1406).

Covers:
1. NexusFederation.share() — share flow orchestration
2. NexusFederation.join() — join flow orchestration
3. CLI argument parsing — _is_federation_syntax, _parse_federation_args
4. Error handling — missing DT_MOUNT, no leader, etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.raft.federation import NexusFederation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeMetadata:
    """Minimal metadata for testing DT_MOUNT discovery."""

    entry_type: int = 0
    is_mount: bool = False
    mount_zone_id: str | None = None


def _make_zone_manager(
    root_zone_id: str = "root",
    node_id: int = 1,
) -> MagicMock:
    mgr = MagicMock()
    mgr.root_zone_id = root_zone_id
    mgr._node_id = node_id
    mgr._py_mgr = MagicMock()
    mgr._py_mgr.advertise_addr.return_value = "alice:2126"
    mgr.share_subtree.return_value = "zone-abc"
    return mgr


def _make_client_factory(
    invite_result: dict[str, Any] | None = None,
    metadata: FakeMetadata | None = None,
    cluster_info: dict[str, Any] | None = None,
    join_result: dict[str, Any] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Return (factory, mock_client) where factory always returns mock_client."""
    client = AsyncMock()
    client.invite_zone.return_value = invite_result or {
        "success": True,
        "node_id": 2,
        "node_address": "bob:2126",
    }
    client.get_metadata.return_value = metadata
    client.get_cluster_info.return_value = cluster_info or {
        "node_id": 1,
        "leader_id": 1,
        "term": 1,
        "is_leader": True,
        "leader_address": "alice:2126",
    }
    client.join_zone.return_value = join_result or {
        "success": True,
        "leader_address": "alice:2126",
        "config": "",
    }
    client.close = AsyncMock()

    factory = MagicMock(return_value=client)
    return factory, client


# ---------------------------------------------------------------------------
# Test 1: Share flow
# ---------------------------------------------------------------------------


class TestShareFlow:
    @pytest.mark.asyncio
    async def test_share_creates_zone_and_invites_peer(self):
        mgr = _make_zone_manager()
        factory, client = _make_client_factory()

        fed = NexusFederation(zone_manager=mgr, client_factory=factory)
        zone_id = await fed.share("/usr/alice/projectA", "bob:2126", "/shared")

        assert zone_id == "zone-abc"

        # Verify share_subtree called with root zone
        mgr.share_subtree.assert_called_once_with(
            parent_zone_id="root",
            path="/usr/alice/projectA",
            zone_id=None,
        )

        # Verify invite_zone called on peer
        client.invite_zone.assert_called_once_with(
            zone_id="zone-abc",
            mount_path="/shared",
            inviter_node_id=1,
            inviter_address="alice:2126",
        )

        # Verify client closed
        client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_share_with_explicit_zone_id(self):
        mgr = _make_zone_manager()
        mgr.share_subtree.return_value = "my-zone"
        factory, _ = _make_client_factory()

        fed = NexusFederation(zone_manager=mgr, client_factory=factory)
        zone_id = await fed.share("/local", "peer:2126", "/remote", zone_id="my-zone")

        assert zone_id == "my-zone"
        mgr.share_subtree.assert_called_once_with(
            parent_zone_id="root",
            path="/local",
            zone_id="my-zone",
        )

    @pytest.mark.asyncio
    async def test_share_client_closed_on_error(self):
        mgr = _make_zone_manager()
        factory, client = _make_client_factory()
        client.invite_zone.side_effect = RuntimeError("connection refused")

        fed = NexusFederation(zone_manager=mgr, client_factory=factory)

        with pytest.raises(RuntimeError, match="connection refused"):
            await fed.share("/local", "peer:2126", "/remote")

        # Client must be closed even on error
        client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2: Join flow
# ---------------------------------------------------------------------------


class TestJoinFlow:
    @pytest.mark.asyncio
    async def test_join_discovers_zone_and_mounts(self):
        mgr = _make_zone_manager()
        metadata = FakeMetadata(is_mount=True, mount_zone_id="zone-xyz")
        factory, client = _make_client_factory(
            metadata=metadata,
            cluster_info={
                "node_id": 1,
                "leader_id": 1,
                "term": 1,
                "is_leader": True,
                "leader_address": "alice:2126",
            },
        )

        fed = NexusFederation(zone_manager=mgr, client_factory=factory)
        zone_id = await fed.join("bob:2126", "/shared", "/usr/charlie/shared")

        assert zone_id == "zone-xyz"

        # Verify discovery calls
        client.get_metadata.assert_called_once_with(path="/shared", zone_id="root")
        client.get_cluster_info.assert_called_once_with(zone_id="zone-xyz")

        # Verify local join
        mgr.join_zone.assert_called_once_with("zone-xyz", peers=["1@alice:2126"])

        # Verify leader notification (second client for leader)
        # factory called twice: once for peer, once for leader
        assert factory.call_count == 2

        # Verify mount
        mgr.mount.assert_called_once_with("root", "/usr/charlie/shared", "zone-xyz")

    @pytest.mark.asyncio
    async def test_join_path_not_found(self):
        mgr = _make_zone_manager()
        factory, _ = _make_client_factory(metadata=None)

        fed = NexusFederation(zone_manager=mgr, client_factory=factory)

        with pytest.raises(ValueError, match="not found on peer"):
            await fed.join("bob:2126", "/nonexistent", "/local")

    @pytest.mark.asyncio
    async def test_join_path_not_mount(self):
        mgr = _make_zone_manager()
        metadata = FakeMetadata(is_mount=False, entry_type=4)
        factory, _ = _make_client_factory(metadata=metadata)

        fed = NexusFederation(zone_manager=mgr, client_factory=factory)

        with pytest.raises(ValueError, match="not a DT_MOUNT"):
            await fed.join("bob:2126", "/regular-dir", "/local")

    @pytest.mark.asyncio
    async def test_join_no_leader(self):
        mgr = _make_zone_manager()
        metadata = FakeMetadata(is_mount=True, mount_zone_id="zone-xyz")
        factory, _ = _make_client_factory(
            metadata=metadata,
            cluster_info={
                "node_id": 1,
                "leader_id": 0,
                "term": 0,
                "is_leader": False,
                "leader_address": None,
            },
        )

        fed = NexusFederation(zone_manager=mgr, client_factory=factory)

        with pytest.raises(RuntimeError, match="no leader"):
            await fed.join("bob:2126", "/shared", "/local")


# ---------------------------------------------------------------------------
# Test 3: CLI argument parsing
# ---------------------------------------------------------------------------


class TestCLIArgumentParsing:
    def test_is_federation_single_arg(self):
        from nexus.cli.commands.server import _is_federation_syntax

        assert _is_federation_syntax("/mnt/nexus", None) is False

    def test_is_federation_share_syntax(self):
        from nexus.cli.commands.server import _is_federation_syntax

        assert _is_federation_syntax("/local", "bob:/remote") is True

    def test_is_federation_join_syntax(self):
        from nexus.cli.commands.server import _is_federation_syntax

        assert _is_federation_syntax("bob:/remote", "/local") is True

    def test_is_federation_no_colon(self):
        from nexus.cli.commands.server import _is_federation_syntax

        assert _is_federation_syntax("/path1", "/path2") is False
