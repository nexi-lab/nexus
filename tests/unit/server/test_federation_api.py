"""Unit tests for federation REST API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.federation import router
from nexus.server.dependencies import require_admin, require_auth

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def mock_zone_manager() -> MagicMock:
    """Mock ZoneManager with standard zone management methods."""
    mgr = MagicMock()
    mgr.node_id = 1
    mgr.list_zones.return_value = ["zone-a", "zone-b"]
    mgr.get_links_count.return_value = 0
    mgr.get_store.return_value = MagicMock()  # non-None store by default
    mgr.create_zone.return_value = None
    mgr.remove_zone.return_value = None
    mgr.mount.return_value = None
    mgr.unmount.return_value = None
    return mgr


@pytest.fixture()
def mock_federation() -> AsyncMock:
    """Mock NexusFederation with share/join coroutines."""
    fed = AsyncMock()
    fed.share.return_value = "shared-zone-id"
    fed.join.return_value = "joined-zone-id"
    return fed


def _auth_override() -> dict:
    return {"subject_id": "user:test", "is_admin": True}


@pytest.fixture()
def app(mock_zone_manager: MagicMock, mock_federation: AsyncMock) -> FastAPI:
    """FastAPI app with federation router and mocked dependencies."""
    test_app = FastAPI()
    test_app.include_router(router)

    # Bypass auth
    test_app.dependency_overrides[require_auth] = _auth_override
    test_app.dependency_overrides[require_admin] = _auth_override

    # Attach mocks to app state
    test_app.state.zone_manager = mock_zone_manager
    test_app.state.federation = mock_federation

    return test_app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    """TestClient bound to the test app."""
    return TestClient(app)


# =============================================================================
# GET /api/v2/federation/zones
# =============================================================================


class TestListZones:
    """Tests for the list_zones endpoint."""

    def test_list_zones(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """GET /zones returns zone list with links_count."""
        mock_zone_manager.list_zones.return_value = ["z1", "z2"]
        mock_zone_manager.get_links_count.side_effect = lambda zid: {
            "z1": 3,
            "z2": 0,
        }[zid]

        resp = client.get("/api/v2/federation/zones")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["zones"]) == 2
        assert body["zones"][0] == {"zone_id": "z1", "links_count": 3}
        assert body["zones"][1] == {"zone_id": "z2", "links_count": 0}
        mock_zone_manager.list_zones.assert_called_once()

    def test_list_zones_empty(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """GET /zones returns empty list when no zones exist."""
        mock_zone_manager.list_zones.return_value = []

        resp = client.get("/api/v2/federation/zones")

        assert resp.status_code == 200
        assert resp.json() == {"zones": []}


# =============================================================================
# GET /api/v2/federation/zones/{zone_id}/cluster-info
# =============================================================================


class TestClusterInfo:
    """Tests for the get_cluster_info endpoint."""

    def test_cluster_info(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """GET /zones/{zone_id}/cluster-info returns zone details."""
        mock_zone_manager.node_id = 42
        mock_zone_manager.get_links_count.return_value = 5
        mock_zone_manager.get_store.return_value = MagicMock()

        resp = client.get("/api/v2/federation/zones/my-zone/cluster-info")

        assert resp.status_code == 200
        body = resp.json()
        assert body["zone_id"] == "my-zone"
        assert body["node_id"] == 42
        assert body["links_count"] == 5
        assert body["has_store"] is True
        mock_zone_manager.get_store.assert_called_once_with("my-zone")

    def test_cluster_info_no_store(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """GET /zones/{zone_id}/cluster-info returns has_store=False when store is None."""
        mock_zone_manager.get_store.return_value = None

        resp = client.get("/api/v2/federation/zones/orphan-zone/cluster-info")

        assert resp.status_code == 200
        assert resp.json()["has_store"] is False


# =============================================================================
# POST /api/v2/federation/zones
# =============================================================================


class TestCreateZone:
    """Tests for the create_zone endpoint."""

    def test_create_zone(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """POST /zones creates a zone and returns 201."""
        resp = client.post(
            "/api/v2/federation/zones",
            json={"zone_id": "new-zone"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["zone_id"] == "new-zone"
        assert body["created"] is True
        mock_zone_manager.create_zone.assert_called_once_with("new-zone", peers=None)

    def test_create_zone_with_peers(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """POST /zones with peers list passes them through."""
        peers = ["2@host1:2126", "3@host2:2126"]

        resp = client.post(
            "/api/v2/federation/zones",
            json={"zone_id": "peer-zone", "peers": peers},
        )

        assert resp.status_code == 201
        mock_zone_manager.create_zone.assert_called_once_with("peer-zone", peers=peers)

    def test_create_zone_conflict(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """POST /zones returns 400 when zone already exists (ValueError)."""
        mock_zone_manager.create_zone.side_effect = ValueError("Zone 'dup' already exists")

        resp = client.post(
            "/api/v2/federation/zones",
            json={"zone_id": "dup"},
        )

        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]


# =============================================================================
# DELETE /api/v2/federation/zones/{zone_id}
# =============================================================================


class TestRemoveZone:
    """Tests for the remove_zone endpoint."""

    def test_remove_zone(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """DELETE /zones/{zone_id} removes zone successfully."""
        resp = client.delete("/api/v2/federation/zones/old-zone")

        assert resp.status_code == 200
        body = resp.json()
        assert body["zone_id"] == "old-zone"
        assert body["removed"] is True
        mock_zone_manager.remove_zone.assert_called_once_with("old-zone", force=False)

    def test_remove_zone_with_force(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """DELETE /zones/{zone_id}?force=true forces removal."""
        resp = client.delete("/api/v2/federation/zones/old-zone?force=true")

        assert resp.status_code == 200
        mock_zone_manager.remove_zone.assert_called_once_with("old-zone", force=True)

    def test_remove_zone_has_links(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """DELETE /zones/{zone_id} returns 400 when zone has references."""
        mock_zone_manager.remove_zone.side_effect = ValueError("Zone has active mount references")

        resp = client.delete("/api/v2/federation/zones/linked-zone")

        assert resp.status_code == 400
        assert "mount references" in resp.json()["detail"]


# =============================================================================
# POST /api/v2/federation/mounts
# =============================================================================


class TestMount:
    """Tests for the mount_zone endpoint."""

    def test_mount(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """POST /mounts creates a mount and returns 201."""
        resp = client.post(
            "/api/v2/federation/mounts",
            json={
                "parent_zone_id": "root",
                "mount_path": "/shared",
                "target_zone_id": "team",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["parent_zone_id"] == "root"
        assert body["mount_path"] == "/shared"
        assert body["target_zone_id"] == "team"
        assert body["mounted"] is True
        mock_zone_manager.mount.assert_called_once_with("root", "/shared", "team")

    def test_mount_invalid_path(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """POST /mounts returns 400 for invalid mount path (ValueError)."""
        mock_zone_manager.mount.side_effect = ValueError("Invalid mount path")

        resp = client.post(
            "/api/v2/federation/mounts",
            json={
                "parent_zone_id": "root",
                "mount_path": "no-leading-slash",
                "target_zone_id": "team",
            },
        )

        assert resp.status_code == 400
        assert "Invalid mount path" in resp.json()["detail"]


# =============================================================================
# DELETE /api/v2/federation/mounts
# =============================================================================


class TestUnmount:
    """Tests for the unmount_zone endpoint."""

    def test_unmount(self, client: TestClient, mock_zone_manager: MagicMock) -> None:
        """DELETE /mounts unmounts a zone."""
        resp = client.request(
            "DELETE",
            "/api/v2/federation/mounts",
            json={
                "parent_zone_id": "root",
                "mount_path": "/shared",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["parent_zone_id"] == "root"
        assert body["mount_path"] == "/shared"
        assert body["unmounted"] is True
        mock_zone_manager.unmount.assert_called_once_with("root", "/shared")


# =============================================================================
# POST /api/v2/federation/share
# =============================================================================


class TestShare:
    """Tests for the share_subtree endpoint."""

    def test_share(self, client: TestClient, mock_federation: AsyncMock) -> None:
        """POST /share shares a subtree and returns 201."""
        mock_federation.share.return_value = "auto-uuid-zone"

        resp = client.post(
            "/api/v2/federation/share",
            json={"local_path": "/usr/alice/projectA"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["zone_id"] == "auto-uuid-zone"
        assert body["local_path"] == "/usr/alice/projectA"
        assert body["shared"] is True
        mock_federation.share.assert_called_once_with(
            local_path="/usr/alice/projectA", zone_id=None
        )

    def test_share_no_federation(self, app: FastAPI) -> None:
        """POST /share returns 503 when federation is not available."""
        app.state.federation = None
        no_fed_client = TestClient(app)

        resp = no_fed_client.post(
            "/api/v2/federation/share",
            json={"local_path": "/data"},
        )

        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]


# =============================================================================
# POST /api/v2/federation/join
# =============================================================================


class TestJoin:
    """Tests for the join_zone endpoint."""

    def test_join(self, client: TestClient, mock_federation: AsyncMock) -> None:
        """POST /join joins a peer zone and returns 201."""
        mock_federation.join.return_value = "peer-zone-abc"

        resp = client.post(
            "/api/v2/federation/join",
            json={
                "peer_addr": "bob:2126",
                "remote_path": "/shared-project",
                "local_path": "/usr/charlie/shared",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["zone_id"] == "peer-zone-abc"
        assert body["peer_addr"] == "bob:2126"
        assert body["local_path"] == "/usr/charlie/shared"
        assert body["joined"] is True
        mock_federation.join.assert_called_once_with(
            peer_addr="bob:2126",
            remote_path="/shared-project",
            local_path="/usr/charlie/shared",
        )

    def test_join_failure(self, client: TestClient, mock_federation: AsyncMock) -> None:
        """POST /join returns 500 on RuntimeError."""
        mock_federation.join.side_effect = RuntimeError("gRPC unreachable")

        resp = client.post(
            "/api/v2/federation/join",
            json={
                "peer_addr": "bad-host:2126",
                "remote_path": "/x",
                "local_path": "/y",
            },
        )

        assert resp.status_code == 500
        assert "gRPC unreachable" in resp.json()["detail"]


# =============================================================================
# Zone manager unavailable (503)
# =============================================================================


class TestZoneManagerUnavailable:
    """Tests for endpoints when zone_manager is None."""

    def test_zone_manager_unavailable(self, app: FastAPI) -> None:
        """Admin zone endpoints return 503 when zone_manager is None."""
        app.state.zone_manager = None
        no_mgr_client = TestClient(app)

        # All admin endpoints that depend on _get_zone_manager should 503
        endpoints = [
            ("GET", "/api/v2/federation/zones"),
            ("GET", "/api/v2/federation/zones/x/cluster-info"),
            ("POST", "/api/v2/federation/zones"),
            ("DELETE", "/api/v2/federation/zones/x"),
            ("POST", "/api/v2/federation/mounts"),
            ("DELETE", "/api/v2/federation/mounts"),
        ]

        for method, path in endpoints:
            if method == "GET":
                resp = no_mgr_client.get(path)
            elif method == "POST":
                resp = no_mgr_client.post(path, json={})
            else:
                resp = no_mgr_client.request(method, path, json={})

            assert resp.status_code == 503, (
                f"{method} {path} expected 503 but got {resp.status_code}"
            )
            assert "not available" in resp.json()["detail"]
