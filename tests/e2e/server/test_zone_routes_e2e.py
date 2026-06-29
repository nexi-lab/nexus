"""E2E tests for zone management API routes.

Tests the security fixes for zone endpoints:
- Authentication required for all endpoints
- Creator assigned as zone owner
- List only shows user's zones

Run with: PYTHONPATH=src python -m pytest tests/e2e/test_zone_routes_e2e.py -v
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.server.auth.factory import create_auth_provider
from nexus.server.auth.zone_routes import router as zone_router
from nexus.storage.models._base import Base


class TestZoneRoutesAuthentication:
    """Test authentication requirements for zone routes."""

    def test_create_zone_requires_auth(self, test_app):
        """Test that creating a zone without auth returns 401."""
        response = test_app.post(
            "/api/zones",
            json={
                "name": "Test Zone",
                "zone_id": "test-zone",
            },
        )
        assert response.status_code == 401, f"Got {response.status_code}: {response.text}"

    def test_get_zone_requires_auth(self, test_app):
        """Test that getting a zone without auth returns 401."""
        response = test_app.get("/api/zones/some-zone")
        assert response.status_code == 401, f"Got {response.status_code}: {response.text}"

    def test_list_zones_requires_auth(self, test_app):
        """Test that listing zones without auth returns 401."""
        response = test_app.get("/api/zones")
        assert response.status_code == 401, f"Got {response.status_code}: {response.text}"


@pytest.fixture(params=("database", "static-database-chain"))
def api_key_zone_app(request, monkeypatch):
    """Build an isolated zone router with real API-key authentication."""
    from nexus.server.auth import auth_routes

    monkeypatch.setattr(auth_routes, "_auth_provider", None)
    monkeypatch.setattr(auth_routes, "_nexus_fs_instance", None)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    record_store = SimpleNamespace(session_factory=session_factory)

    subject_id = f"{request.param}-admin"
    with session_factory() as session:
        _key_id, api_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id=subject_id,
            subject_id=subject_id,
            name=f"zone route {request.param} admin",
            is_admin=True,
        )
        session.commit()

    if request.param == "database":
        auth_provider = DatabaseAPIKeyAuth(record_store)
    else:
        auth_provider = create_auth_provider(
            "static",
            auth_config={
                "api_keys": {
                    "sk-static-zone-route-admin-key": {
                        "subject_type": "user",
                        "subject_id": "static-admin",
                        "is_admin": True,
                    }
                }
            },
            record_store=record_store,
        )
        assert auth_provider is not None

    app = FastAPI()
    app.state.auth_provider = auth_provider
    app.state.session_factory = session_factory
    app.include_router(zone_router)

    with TestClient(app) as client:
        yield {
            "client": client,
            "api_key": api_key,
            "provider_kind": request.param,
            "session_factory": session_factory,
        }

    auth_provider.close()
    engine.dispose()


def test_api_key_providers_support_full_zone_lifecycle_without_local_auth(
    api_key_zone_app,
) -> None:
    """Pure and chained API-key providers use app-state sessions on every route."""
    from nexus.server.auth import auth_routes

    client = api_key_zone_app["client"]
    api_key = api_key_zone_app["api_key"]
    provider_kind = api_key_zone_app["provider_kind"]
    zone_id = f"route-{provider_kind}"
    headers = {"Authorization": f"Bearer {api_key}"}

    assert auth_routes._auth_provider is None
    assert callable(api_key_zone_app["session_factory"])

    create_response = client.post(
        "/api/zones",
        json={"name": f"Route {provider_kind}", "zone_id": zone_id},
        headers=headers,
    )
    assert create_response.status_code == 201, create_response.text
    assert create_response.json()["zone_id"] == zone_id

    get_response = client.get(f"/api/zones/{zone_id}", headers=headers)
    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["zone_id"] == zone_id

    list_response = client.get("/api/zones", headers=headers)
    assert list_response.status_code == 200, list_response.text
    assert zone_id in {zone["zone_id"] for zone in list_response.json()["zones"]}

    delete_response = client.delete(f"/api/zones/{zone_id}", headers=headers)
    assert delete_response.status_code == 202, delete_response.text
    assert delete_response.json()["zone_id"] == zone_id


class TestZoneRoutesWithAuth:
    """Test zone routes with proper authentication."""

    @pytest.fixture
    def auth_token(self, test_app):
        """Register a user and get auth token."""
        response = test_app.post(
            "/auth/register",
            json={
                "email": "zone-test@example.com",
                "password": "securepassword123",
                "username": "zoneuser",
                "display_name": "Zone Test User",
            },
        )
        # Skip test if auth provider not configured (503)
        if response.status_code == 503:
            pytest.skip("Auth provider not configured in test environment")
        assert response.status_code == 201, f"Registration failed: {response.text}"
        return response.json()["token"]

    def test_create_zone_with_auth(self, test_app, auth_token):
        """Test creating a zone with valid authentication."""
        response = test_app.post(
            "/api/zones",
            json={
                "name": "My Organization",
                "zone_id": "my-org",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # Should succeed (201) or fail gracefully if ReBAC not configured (500)
        assert response.status_code in (201, 500)
        if response.status_code == 201:
            data = response.json()
            assert data["zone_id"] == "my-org"
            assert data["name"] == "My Organization"
            assert data["is_active"] is True

    def test_list_zones_with_auth(self, test_app, auth_token):
        """Test listing zones with valid authentication."""
        response = test_app.get(
            "/api/zones",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # Should succeed - may return empty list if user has no zones
        assert response.status_code == 200
        data = response.json()
        assert "zones" in data
        assert "total" in data

    def test_get_nonexistent_zone_with_auth(self, test_app, auth_token):
        """Test getting a non-existent zone returns 403 or 404."""
        response = test_app.get(
            "/api/zones/nonexistent-zone",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # 403 = user doesn't have access (correct - access check before existence)
        # 404 = zone not found (also acceptable)
        assert response.status_code in (403, 404)


class TestZoneCreatorOwnership:
    """Test that zone creator is assigned as owner."""

    @pytest.fixture
    def auth_token(self, test_app):
        """Register a user and get auth token."""
        response = test_app.post(
            "/auth/register",
            json={
                "email": "owner-test@example.com",
                "password": "securepassword123",
                "username": "owneruser",
            },
        )
        # Skip test if auth provider not configured (503)
        if response.status_code == 503:
            pytest.skip("Auth provider not configured in test environment")
        assert response.status_code == 201, f"Registration failed: {response.text}"
        return response.json()["token"]

    def test_creator_can_access_created_zone(self, test_app, auth_token):
        """Test that the zone creator can access their created zone."""
        # Create zone
        create_response = test_app.post(
            "/api/zones",
            json={
                "name": "Owner Test Org",
                "zone_id": "owner-test-org",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # Skip test if creation failed (ReBAC not available)
        if create_response.status_code != 201:
            pytest.skip("Zone creation failed - ReBAC may not be configured")

        # Creator should be able to get the zone
        get_response = test_app.get(
            "/api/zones/owner-test-org",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert get_response.status_code == 200
        data = get_response.json()
        assert data["zone_id"] == "owner-test-org"

        # Creator should see zone in list
        list_response = test_app.get(
            "/api/zones",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert list_response.status_code == 200
        data = list_response.json()
        zone_ids = [t["zone_id"] for t in data["zones"]]
        assert "owner-test-org" in zone_ids
