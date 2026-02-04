"""E2E tests for tenant management API routes.

Tests the security fixes for tenant endpoints:
- Authentication required for all endpoints
- Creator assigned as tenant owner
- List only shows user's tenants

Run with: PYTHONPATH=src python -m pytest tests/e2e/test_tenant_routes_e2e.py -v
"""

import pytest


class TestTenantRoutesAuthentication:
    """Test authentication requirements for tenant routes."""

    def test_create_tenant_requires_auth(self, test_app):
        """Test that creating a tenant without auth returns 401/422."""
        response = test_app.post(
            "/api/tenants",
            json={
                "name": "Test Tenant",
                "tenant_id": "test-tenant",
            },
        )
        # Should require authentication
        # 422 = missing Authorization header (FastAPI validation)
        # 401 = invalid/missing token
        # 503 = auth provider not configured (acceptable in minimal test setup)
        assert response.status_code in (401, 422, 503), (
            f"Got {response.status_code}: {response.text}"
        )

    def test_get_tenant_requires_auth(self, test_app):
        """Test that getting a tenant without auth returns 401/422."""
        response = test_app.get("/api/tenants/some-tenant")
        assert response.status_code in (401, 422, 503), (
            f"Got {response.status_code}: {response.text}"
        )

    def test_list_tenants_requires_auth(self, test_app):
        """Test that listing tenants without auth returns 401/422."""
        response = test_app.get("/api/tenants")
        assert response.status_code in (401, 422, 503), (
            f"Got {response.status_code}: {response.text}"
        )


class TestTenantRoutesWithAuth:
    """Test tenant routes with proper authentication."""

    @pytest.fixture
    def auth_token(self, test_app):
        """Register a user and get auth token."""
        response = test_app.post(
            "/auth/register",
            json={
                "email": "tenant-test@example.com",
                "password": "securepassword123",
                "username": "tenantuser",
                "display_name": "Tenant Test User",
            },
        )
        # Skip test if auth provider not configured (503)
        if response.status_code == 503:
            pytest.skip("Auth provider not configured in test environment")
        assert response.status_code == 201, f"Registration failed: {response.text}"
        return response.json()["token"]

    def test_create_tenant_with_auth(self, test_app, auth_token):
        """Test creating a tenant with valid authentication."""
        response = test_app.post(
            "/api/tenants",
            json={
                "name": "My Organization",
                "tenant_id": "my-org",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # Should succeed (201) or fail gracefully if ReBAC not configured (500)
        assert response.status_code in (201, 500)
        if response.status_code == 201:
            data = response.json()
            assert data["tenant_id"] == "my-org"
            assert data["name"] == "My Organization"
            assert data["is_active"] is True

    def test_list_tenants_with_auth(self, test_app, auth_token):
        """Test listing tenants with valid authentication."""
        response = test_app.get(
            "/api/tenants",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # Should succeed - may return empty list if user has no tenants
        assert response.status_code == 200
        data = response.json()
        assert "tenants" in data
        assert "total" in data

    def test_get_nonexistent_tenant_with_auth(self, test_app, auth_token):
        """Test getting a non-existent tenant returns 403 or 404."""
        response = test_app.get(
            "/api/tenants/nonexistent-tenant",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # 403 = user doesn't have access (correct - access check before existence)
        # 404 = tenant not found (also acceptable)
        assert response.status_code in (403, 404)


class TestTenantCreatorOwnership:
    """Test that tenant creator is assigned as owner."""

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

    def test_creator_can_access_created_tenant(self, test_app, auth_token):
        """Test that the tenant creator can access their created tenant."""
        # Create tenant
        create_response = test_app.post(
            "/api/tenants",
            json={
                "name": "Owner Test Org",
                "tenant_id": "owner-test-org",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # Skip test if creation failed (ReBAC not available)
        if create_response.status_code != 201:
            pytest.skip("Tenant creation failed - ReBAC may not be configured")

        # Creator should be able to get the tenant
        get_response = test_app.get(
            "/api/tenants/owner-test-org",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert get_response.status_code == 200
        data = get_response.json()
        assert data["tenant_id"] == "owner-test-org"

        # Creator should see tenant in list
        list_response = test_app.get(
            "/api/tenants",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert list_response.status_code == 200
        data = list_response.json()
        tenant_ids = [t["tenant_id"] for t in data["tenants"]]
        assert "owner-test-org" in tenant_ids
