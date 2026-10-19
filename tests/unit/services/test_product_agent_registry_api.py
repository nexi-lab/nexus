"""Exercise the HTTP boundary with real Nexus authentication and SQLite storage."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.product_agent_registry import router
from tests.unit.services.test_product_agent_registry import (
    command,
    ready,
)
from tests.unit.services.test_product_agent_registry import (
    registry as registry,
)

BASE = "/api/v2/product-agents"
HEADERS = {"Authorization": "Bearer sk-test-registry-operator", "X-Nexus-Zone-ID": "zone-a"}


class Provider:
    async def authenticate(self, token):
        if token != "sk-test-member":
            return None
        return SimpleNamespace(
            authenticated=True,
            is_admin=False,
            subject_type="user",
            subject_id="member",
            zone_id="zone-a",
            zone_set=(),
            zone_perms=(),
            inherit_permissions=True,
            metadata={},
            agent_generation=None,
        )


@pytest.fixture
def client(registry):
    app = FastAPI()
    app.state.product_agent_registry = registry
    app.state.api_key = "sk-test-registry-operator"
    app.state.auth_provider = Provider()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_http_command_and_read_use_authenticated_identity(client):
    request = command("RegisterPrincipal", display_name="Assistant").model_dump()
    result = client.post(BASE + "/commands", headers=HEADERS, json=request)
    assert result.status_code == 200
    assert result.json()["principal"]["owner_zone"] == "zone-a"
    assert (
        client.get(BASE + "/agents.example/assistant/versions", headers=HEADERS).status_code == 200
    )
    assert client.post(BASE + "/commands", headers=HEADERS, json=request).json() == result.json()


def test_member_cannot_escalate_using_headers_or_body(client, registry):
    ready(registry)
    headers = {
        "Authorization": "Bearer sk-test-member",
        "X-Nexus-Subject": "user:admin",
        "X-Nexus-Zone-ID": "zone-a",
    }
    assert (
        client.get(BASE + "/agents.example/assistant/versions", headers=headers).status_code == 200
    )
    response = client.post(
        BASE + "/commands", headers=headers, json={"is_admin": True, "subject_id": "admin"}
    )
    assert response.status_code == 403
    headers["X-Nexus-Zone-ID"] = "zone-b"
    assert (
        client.get(BASE + "/agents.example/assistant/versions", headers=headers).status_code == 401
    )


def test_open_access_and_missing_auth_are_rejected(client):
    assert client.get(BASE + "/agents.example/assistant/versions").status_code == 401
    client.app.state.api_key = None
    client.app.state.auth_provider = None
    response = client.get(
        BASE + "/agents.example/assistant/versions",
        headers={"X-Nexus-Subject": "user:admin", "X-Nexus-Zone-ID": "zone-a"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "REGISTRY_IDENTITY_REQUIRED"


def test_invalid_inputs_do_not_echo_payloads(client, registry):
    ready(registry)
    for body in [b'{"private_value":"do-not-echo"}', b'{"kind":"a","kind":"b"}']:
        response = client.post(BASE + "/commands", headers=HEADERS, content=body)
        assert response.status_code == 400
        assert "do-not-echo" not in response.text
    assert client.post(BASE + "/commands", headers=HEADERS, content=b"x" * 65537).status_code == 413
    for query in [
        {"version": ""},
        {"version": "do-not-echo/"},
        {"channel": "do-not-echo"},
        {"version": "v1", "channel": "stable"},
    ]:
        response = client.get(
            BASE + "/agents.example/assistant/resolve", headers=HEADERS, params=query
        )
        assert response.status_code == 400
        assert "do-not-echo" not in response.text
    assert (
        client.get(
            BASE + "/agents.example/assistant/versions",
            headers=HEADERS,
            params={"after": "x" * 129},
        ).status_code
        == 400
    )


def test_unconfigured_and_storage_errors_fail_closed(client, registry):
    from sqlalchemy.exc import OperationalError

    ready(registry)
    original = registry.resolve

    def unavailable(*_args):
        raise OperationalError("query with private-value", {}, None)

    registry.resolve = unavailable
    response = client.get(BASE + "/agents.example/assistant/resolve", headers=HEADERS)
    assert response.status_code == 503 and "private-value" not in response.text
    registry.resolve = original
    client.app.state.product_agent_registry = None
    assert (
        client.get(BASE + "/agents.example/assistant/versions", headers=HEADERS).status_code == 503
    )
