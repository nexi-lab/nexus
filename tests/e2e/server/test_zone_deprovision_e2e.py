"""E2E tests for zone deprovisioning via DELETE /api/zones/{zone_id} (Issue #2061).

Tests the full zone lifecycle: create → deprovision → verify cleanup.
Requires a running nexus server (test_app fixture from conftest.py).
"""

from __future__ import annotations

import pytest


class TestZoneDeprovisionAuthentication:
    """Authentication requirements for DELETE /api/zones/{zone_id}."""

    def test_delete_zone_requires_auth(self, test_app):
        """DELETE without auth returns 401/422."""
        response = test_app.delete("/api/zones/some-zone")
        assert response.status_code in (401, 422, 503), (
            f"Got {response.status_code}: {response.text}"
        )


class TestZoneDeprovisionFlow:
    """Full deprovision flow with authentication."""

    @pytest.fixture
    def auth_token(self, test_app):
        """Register a user and get auth token."""
        response = test_app.post(
            "/auth/register",
            json={
                "email": "deprovision-test@example.com",
                "password": "securepassword123",
                "username": "deprovisionuser",
                "display_name": "Deprovision Test User",
            },
        )
        if response.status_code == 503:
            pytest.skip("Auth provider not configured in test environment")
        assert response.status_code == 201, f"Registration failed: {response.text}"
        return response.json()["token"]

    @pytest.fixture
    def zone_id(self, test_app, auth_token):
        """Create a zone for deprovision testing."""
        response = test_app.post(
            "/api/zones",
            json={"name": "Deprovision Zone", "zone_id": "deprovision-test"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        if response.status_code != 201:
            pytest.skip("Zone creation failed — ReBAC may not be configured")
        return response.json()["zone_id"]

    def test_deprovision_active_zone(self, test_app, auth_token, zone_id):
        """DELETE on Active zone returns 202 Accepted."""
        response = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # 202 = finalization started, 503 = lifecycle service unavailable
        assert response.status_code in (202, 503), (
            f"Got {response.status_code}: {response.text}"
        )
        if response.status_code == 202:
            data = response.json()
            assert data["zone_id"] == zone_id
            assert data["phase"] in ("Terminating", "Terminated")

    def test_get_zone_after_deprovision(self, test_app, auth_token, zone_id):
        """After deprovision, GET shows phase != Active."""
        # Deprovision
        del_response = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        if del_response.status_code not in (202,):
            pytest.skip("Deprovision not available in test env")

        # GET the zone — should still be accessible but phase changed
        get_response = test_app.get(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        # May be 200 (Terminating/Terminated) or 404 (already cleaned)
        if get_response.status_code == 200:
            data = get_response.json()
            assert data["phase"] in ("Terminating", "Terminated")
            assert data["is_active"] is False

    def test_double_delete_is_idempotent(self, test_app, auth_token, zone_id):
        """Second DELETE on same zone is idempotent (retry or already terminated)."""
        # First DELETE
        first = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        if first.status_code not in (202,):
            pytest.skip("Deprovision not available in test env")

        # Second DELETE — if Terminating → 202 (retry), if Terminated → 404
        second = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert second.status_code in (202, 404), (
            f"Expected 202 or 404 on double delete, got {second.status_code}: {second.text}"
        )

    def test_delete_nonexistent_zone(self, test_app, auth_token):
        """DELETE on non-existent zone returns 403 or 404."""
        response = test_app.delete(
            "/api/zones/nonexistent-zone",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code in (403, 404), (
            f"Got {response.status_code}: {response.text}"
        )


class TestZoneResponseFormat:
    """Verify zone responses include phase and finalizers fields."""

    @pytest.fixture
    def auth_token(self, test_app):
        """Register a user and get auth token."""
        response = test_app.post(
            "/auth/register",
            json={
                "email": "format-test@example.com",
                "password": "securepassword123",
                "username": "formatuser",
            },
        )
        if response.status_code == 503:
            pytest.skip("Auth provider not configured")
        assert response.status_code == 201
        return response.json()["token"]

    def test_create_zone_includes_phase(self, test_app, auth_token):
        """POST /api/zones response includes phase and finalizers."""
        response = test_app.post(
            "/api/zones",
            json={"name": "Format Test Zone", "zone_id": "format-test"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        if response.status_code != 201:
            pytest.skip("Zone creation failed")
        data = response.json()
        assert data["phase"] == "Active"
        assert data["finalizers"] == []
        assert data["is_active"] is True
