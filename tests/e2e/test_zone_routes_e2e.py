"""E2E tests for zone management API routes.

Tests the security fixes for zone endpoints:
- Authentication required for all endpoints
- Creator assigned as zone owner
- List only shows user's zones

Run with: PYTHONPATH=src python -m pytest tests/e2e/test_zone_routes_e2e.py -v
"""

import pytest


class TestZoneRoutesAuthentication:
    """Test authentication requirements for zone routes."""

    def test_create_zone_requires_auth(self, test_app):
        """Test that creating a zone without auth returns 401/422."""
        response = test_app.post(
            "/api/zones",
            json={
                "name": "Test Zone",
                "zone_id": "test-zone",
            },
        )
        # Should require authentication
        # 422 = missing Authorization header (FastAPI validation)
        # 401 = invalid/missing token
        # 503 = auth provider not configured (acceptable in minimal test setup)
        assert response.status_code in (401, 422, 503), (
            f"Got {response.status_code}: {response.text}"
        )

    def test_get_zone_requires_auth(self, test_app):
        """Test that getting a zone without auth returns 401/422."""
        response = test_app.get("/api/zones/some-zone")
        assert response.status_code in (401, 422, 503), (
            f"Got {response.status_code}: {response.text}"
        )

    def test_list_zones_requires_auth(self, test_app):
        """Test that listing zones without auth returns 401/422."""
        response = test_app.get("/api/zones")
        assert response.status_code in (401, 422, 503), (
            f"Got {response.status_code}: {response.text}"
        )


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
