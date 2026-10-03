"""E2E tests for zone management API routes.

Tests the security fixes for zone endpoints:
- Authentication required for all endpoints
- Creator assigned as zone owner
- List only shows user's zones

Run with: PYTHONPATH=src python -m pytest tests/e2e/test_zone_routes_e2e.py -v
"""

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.remote.zone_runtime_client import RuntimeReceipt
from nexus.server.auth.factory import create_auth_provider
from nexus.server.auth.zone_routes import router as zone_router
from nexus.server.lifespan.zone_control import arm_zone_services
from nexus.storage.models._base import Base


def _wait_for_operation(client, location: str, headers: dict[str, str]) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(location, headers=headers)
        assert response.status_code == 200, response.text
        operation = response.json()
        if operation["state"] in {"succeeded", "failed"}:
            return operation
        time.sleep(0.1)
    pytest.fail(f"operation did not complete: {location}")


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
    app.state.api_key = None
    app.state.auth_provider = auth_provider
    app.state.session_factory = session_factory
    runtime = SimpleNamespace(
        probe_capabilities=lambda **_: (
            "zone-runtime:create",
            "zone-runtime:join",
            "zone-runtime:status",
            "zone-runtime:mount",
            "zone-runtime:unmount",
            "zone-runtime:deprovision",
            "zone-runtime:operation-journal",
        ),
        create_zone=lambda **kwargs: RuntimeReceipt(
            ok=True,
            physical_identity=kwargs["zone_id"],
            membership="RESIDENT",
            raw={"zone_id": kwargs["zone_id"], "outcome": "CREATED"},
        ),
        zone_status=lambda **kwargs: RuntimeReceipt(
            ok=True,
            physical_identity=kwargs["zone_id"],
            membership="RESIDENT",
            runtime_revision="1:1:1",
            raw={"zone_id": kwargs["zone_id"], "presence": "RESIDENT"},
        ),
        deprovision=lambda **kwargs: RuntimeReceipt(
            ok=True,
            physical_identity=kwargs["zone_id"],
            membership="DELETED",
            raw={"zone_id": kwargs["zone_id"], "outcome": "DEPROVISIONED"},
        ),
    )
    arm_zone_services(
        app,
        session_factory=session_factory,
        runtime=runtime,
        rebac_check=lambda *_args: True,
        projection_write=lambda *_args: None,
        projection_delete=lambda *_args: None,
        membership_check=lambda *_args: True,
        worker_enabled=True,
    )
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
    def auth_token(self, nexus_server):
        """Use the real key minted into both Python and Rust auth planes."""
        return nexus_server["api_key"]

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
        assert response.status_code == 202, response.text
        operation = _wait_for_operation(
            test_app,
            response.headers["Location"],
            {"Authorization": f"Bearer {auth_token}"},
        )
        assert operation["state"] == "succeeded", operation
        current = test_app.get(
            "/api/zones/my-org", headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert current.status_code == 200, current.text
        data = current.json()
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
        assert response.status_code == 404


class TestZoneCreatorOwnership:
    """Test that zone creator is assigned as owner."""

    @pytest.fixture
    def auth_token(self, nexus_server):
        """Use the real key minted into both Python and Rust auth planes."""
        return nexus_server["api_key"]

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

        assert create_response.status_code == 202, create_response.text
        operation = _wait_for_operation(
            test_app,
            create_response.headers["Location"],
            {"Authorization": f"Bearer {auth_token}"},
        )
        assert operation["state"] == "succeeded", operation

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


def test_v2_create_delegation_revoke_and_deprovision(nexus_server, test_app) -> None:
    """Exercise the public /v2 saga with a user's own short-lived delegation."""
    admin_headers = {"Authorization": f"Bearer {nexus_server['api_key']}"}
    zone_id = "v2-delegation-zone"

    capabilities = test_app.get("/v2/zone-capabilities", headers=admin_headers)
    assert capabilities.status_code == 200, capabilities.text
    providers = capabilities.json()["providers"]
    assert providers["composite_armed"] is True
    assert providers["auth_armed"] is True
    assert providers["rebac_armed"] is True

    created = test_app.post(
        "/v2/zones",
        headers={**admin_headers, "Idempotency-Key": "create-v2-delegation-zone"},
        json={"zone_id": zone_id, "display_name": "V2 Delegation Zone"},
    )
    assert created.status_code == 202, created.text
    create_operation = _wait_for_operation(test_app, created.headers["Location"], admin_headers)
    assert create_operation["state"] == "succeeded", create_operation

    service_key_response = test_app.post(
        "/api/v2/auth/keys",
        headers=admin_headers,
        json={
            "label": "moss-e2e",
            "subject_type": "service",
            "subject_id": "moss-e2e",
            "zone_id": "root",
            "is_admin": True,
        },
    )
    assert service_key_response.status_code == 201, service_key_response.text
    service_key = service_key_response.json()["key"]

    user_key_response = test_app.post(
        "/api/v2/auth/keys",
        headers=admin_headers,
        json={
            "label": "delegated-user",
            "subject_type": "user",
            "subject_id": "delegated-user",
            "zone_id": zone_id,
            "is_admin": False,
        },
    )
    assert user_key_response.status_code == 201, user_key_response.text
    user_key = user_key_response.json()["key"]

    grant_response = test_app.post(
        f"/v2/zones/{zone_id}/grants",
        headers={**admin_headers, "Idempotency-Key": "grant-v2-org"},
        json={
            "grantee": {"subject_type": "organization", "subject_id": "org-e2e"},
            "capabilities": ["zone.data.read"],
            "resource_prefixes": ["/"],
            "source": {"source_type": "moss_org_binding", "source_id": "binding-e2e"},
            "reason": "accepted organization binding",
        },
    )
    assert grant_response.status_code == 202, grant_response.text
    grant_operation = _wait_for_operation(
        test_app, grant_response.headers["Location"], admin_headers
    )
    assert grant_operation["state"] == "succeeded", grant_operation

    grants_response = test_app.get(f"/v2/zones/{zone_id}/grants", headers=admin_headers)
    assert grants_response.status_code == 200, grants_response.text
    org_grant = next(
        grant
        for grant in grants_response.json()["grants"]
        if grant["grantee"]["subject_id"] == "org-e2e"
    )
    assert org_grant["status"] == "active"

    delegation_response = test_app.post(
        "/v2/auth/zone-delegations",
        headers={"Authorization": f"Bearer {service_key}", "Idempotency-Key": "delegate-e2e"},
        json={
            "user_id": "delegated-user",
            "org_id": "org-e2e",
            "membership_version": "r1",
            "zone_id": zone_id,
            "audience": "nexus-api",
            "ttl_s": 300,
        },
    )
    assert delegation_response.status_code == 201, delegation_response.text
    delegation_id = delegation_response.json()["delegation_id"]

    user_headers = {
        "Authorization": f"Bearer {user_key}",
        "X-Nexus-Zone-Delegation": delegation_id,
    }
    assert test_app.get(f"/v2/zones/{zone_id}", headers=user_headers).status_code == 200

    collision_key_response = test_app.post(
        "/api/v2/auth/keys",
        headers=admin_headers,
        json={
            "label": "delegated-service-collision",
            "subject_type": "service",
            "subject_id": "delegated-user",
            "zone_id": zone_id,
            "is_admin": False,
        },
    )
    assert collision_key_response.status_code == 201, collision_key_response.text
    collision = test_app.get(
        f"/v2/zones/{zone_id}",
        headers={
            "Authorization": f"Bearer {collision_key_response.json()['key']}",
            "X-Nexus-Zone-Delegation": delegation_id,
        },
    )
    assert collision.status_code == 403, collision.text

    revoked = test_app.delete(
        f"/v2/zones/{zone_id}/grants/{org_grant['grant_id']}",
        headers={**admin_headers, "Idempotency-Key": "revoke-v2-org"},
    )
    assert revoked.status_code == 202, revoked.text
    denied = test_app.get(f"/v2/zones/{zone_id}", headers=user_headers)
    assert denied.status_code == 403, denied.text

    deleted = test_app.delete(
        f"/v2/zones/{zone_id}",
        headers={
            **admin_headers,
            "Idempotency-Key": "delete-v2-delegation-zone",
            "X-Nexus-Confirm-Zone": zone_id,
        },
    )
    assert deleted.status_code == 202, (
        deleted.text + "\n" + "".join(nexus_server["stderr_lines"][-120:])
    )
    delete_operation = _wait_for_operation(test_app, deleted.headers["Location"], admin_headers)
    assert delete_operation["state"] == "succeeded", delete_operation
