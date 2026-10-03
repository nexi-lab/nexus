"""E2E tests for zone deprovisioning via DELETE /api/zones/{zone_id} (Issue #2061).

Tests the full zone lifecycle: create → deprovision → verify cleanup.
Requires a running nexus server (test_app fixture from conftest.py).
"""

import time

import pytest


def _wait_for_operation(test_app, location: str, headers: dict[str, str]) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = test_app.get(location, headers=headers)
        assert response.status_code == 200, response.text
        operation = response.json()
        if operation["state"] in {"succeeded", "failed"}:
            return operation
        time.sleep(0.1)
    pytest.fail(f"operation did not complete: {location}")


class TestZoneDeprovisionAuthentication:
    """Authentication requirements for DELETE /api/zones/{zone_id}."""

    def test_delete_zone_requires_auth(self, test_app):
        """DELETE without auth returns 401."""
        response = test_app.delete("/api/zones/some-zone")
        assert response.status_code == 401, f"Got {response.status_code}: {response.text}"


class TestZoneDeprovisionFlow:
    """Full deprovision flow with authentication."""

    @pytest.fixture
    def auth_token(self, nexus_server):
        """Use the real key minted into both Python and Rust auth planes."""
        return nexus_server["api_key"]

    @pytest.fixture
    def zone_id(self, test_app, auth_token):
        """Create a zone for deprovision testing."""
        response = test_app.post(
            "/api/zones",
            json={"name": "Deprovision Zone", "zone_id": "deprovision-test"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 202, response.text
        operation = _wait_for_operation(
            test_app,
            response.headers["Location"],
            {"Authorization": f"Bearer {auth_token}"},
        )
        assert operation["state"] == "succeeded", operation
        return response.json()["zone_id"]

    def test_deprovision_active_zone(self, test_app, auth_token, zone_id):
        """DELETE on Active zone returns 202 Accepted."""
        response = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 202, f"Got {response.status_code}: {response.text}"
        data = response.json()
        assert data["zone_id"] == zone_id
        assert data["phase"] == "Terminating"

    def test_get_zone_after_deprovision(self, test_app, auth_token, zone_id):
        """After deprovision, GET shows phase != Active."""
        # Deprovision
        del_response = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert del_response.status_code == 202, del_response.text
        operation = _wait_for_operation(
            test_app,
            del_response.headers["Location"],
            {"Authorization": f"Bearer {auth_token}"},
        )
        assert operation["state"] == "succeeded", operation

        # GET the zone — should still be accessible but phase changed
        get_response = test_app.get(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert get_response.status_code == 200, get_response.text
        data = get_response.json()
        assert data["phase"] == "Terminated"
        assert data["is_active"] is False

    def test_double_delete_is_idempotent(self, test_app, auth_token, zone_id):
        """Second DELETE on same zone is idempotent (retry or already terminated)."""
        # First DELETE
        first = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert first.status_code == 202, first.text

        # Second DELETE — if Terminating → 202 (retry), if Terminated → 404
        second = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert second.status_code == 202, second.text
        assert second.headers["Location"] == first.headers["Location"]

    def test_delete_nonexistent_zone(self, test_app, auth_token):
        """DELETE on non-existent zone returns 404 for the global admin."""
        response = test_app.delete(
            "/api/zones/nonexistent-zone",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 404, f"Got {response.status_code}: {response.text}"


class TestZoneDeprovisionDBState:
    """Verify post-deprovision database state (#12A — Issue #2070)."""

    @pytest.fixture
    def auth_token(self, nexus_server):
        """Use the real key minted into both Python and Rust auth planes."""
        return nexus_server["api_key"]

    @pytest.fixture
    def zone_id(self, test_app, auth_token):
        """Create a zone for DB state testing."""
        response = test_app.post(
            "/api/zones",
            json={"name": "DB State Zone", "zone_id": "db-state-test"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 202, response.text
        operation = _wait_for_operation(
            test_app,
            response.headers["Location"],
            {"Authorization": f"Bearer {auth_token}"},
        )
        assert operation["state"] == "succeeded", operation
        return response.json()["zone_id"]

    def test_deprovision_sets_phase_and_finalizers(self, test_app, auth_token, zone_id):
        """After deprovision, zone phase is Terminating/Terminated and finalizers updated."""
        # Deprovision
        del_response = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert del_response.status_code == 202, del_response.text

        data = del_response.json()
        # Must have the phase transition
        assert data["phase"] == "Terminating"
        # Completed + pending should cover all registered finalizers
        assert isinstance(data["finalizers_completed"], list)
        assert isinstance(data["finalizers_pending"], list)
        assert isinstance(data["finalizers_failed"], dict)

    def test_terminated_zone_not_in_list(self, test_app, auth_token, zone_id):
        """Terminated zone is excluded from list_zones (phase != Terminated filter)."""
        # Deprovision
        del_response = test_app.delete(
            f"/api/zones/{zone_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert del_response.status_code == 202, del_response.text
        operation = _wait_for_operation(
            test_app,
            del_response.headers["Location"],
            {"Authorization": f"Bearer {auth_token}"},
        )
        assert operation["state"] == "succeeded", operation

        # List zones should NOT include terminated zone
        list_response = test_app.get(
            "/api/zones",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert list_response.status_code == 200, list_response.text

        zone_ids = [z["zone_id"] for z in list_response.json()["zones"]]
        assert zone_id not in zone_ids


class TestZoneResponseFormat:
    """Verify zone responses include phase and finalizers fields."""

    @pytest.fixture
    def auth_token(self, nexus_server):
        """Use the real key minted into both Python and Rust auth planes."""
        return nexus_server["api_key"]

    def test_create_zone_includes_phase(self, test_app, auth_token):
        """POST /api/zones response includes phase and finalizers."""
        response = test_app.post(
            "/api/zones",
            json={"name": "Format Test Zone", "zone_id": "format-test"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 202, response.text
        operation = _wait_for_operation(
            test_app,
            response.headers["Location"],
            {"Authorization": f"Bearer {auth_token}"},
        )
        assert operation["state"] == "succeeded", operation
        current = test_app.get(
            "/api/zones/format-test",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert current.status_code == 200, current.text
        data = current.json()
        assert data["phase"] == "Active"
        assert data["finalizers"] == []
        assert data["is_active"] is True
