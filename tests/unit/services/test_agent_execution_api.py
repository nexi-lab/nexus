"""Execution API against real IAM credentials and persistent Registry state."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.server.api.v2.routers.product_agent_registry import router
from nexus.storage.api_key_ops import create_api_key
from tests.unit.services.test_agent_execution import MODEL, request
from tests.unit.services.test_agent_execution import execution as execution
from tests.unit.services.test_product_agent_registry import ADMIN

BASE = "/api/v2/product-agents/execution"


@pytest.fixture
def api(execution):
    app = FastAPI()
    app.state.product_agent_registry = execution.registry
    app.state.product_agent_execution = execution
    app.state.auth_provider = DatabaseAPIKeyAuth(execution.auth._record_store)
    app.state.api_key = None
    app.include_router(router)
    credentials = {}
    with execution.auth.session_factory() as session:
        for label, subject, zone, admin in [
            ("operator", "admin", "zone-a", True),
            ("member", "member", "zone-a", False),
            ("foreign", "admin", "zone-b", True),
        ]:
            _, key = create_api_key(
                session,
                user_id=subject,
                subject_id=subject,
                subject_type="user",
                name=label,
                zone_id=zone,
                is_admin=admin,
            )
            credentials[label] = {"Authorization": "Bearer " + key, "X-Nexus-Zone-ID": zone}
        session.commit()
    with TestClient(app) as client:
        yield client, credentials


def test_issue_authorize_revoke_http_boundary(api, execution):
    client, credentials = api
    headers = credentials["operator"]
    req = request(execution).model_dump()
    response = client.post(BASE + "/grants", headers=headers, json=req)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    issued = response.json()
    key, grant = issued["api_key"], issued["grant"]
    auth_check = {
        "grant_id": grant["id"],
        "task_id": req["task_id"],
        "spec_digest": req["spec_digest"],
        "permission": MODEL.model_dump(),
        "token": key,
    }
    response = client.post(BASE + "/authorize", headers=headers, json=auth_check)
    assert response.status_code == 200 and key not in response.text
    assert response.json()["cacheable"] is False
    assert client.post(BASE + "/grants", headers=headers, json=req).json()["api_key"] is None
    response = client.get(BASE + "/grants/" + grant["id"], headers=headers)
    assert response.status_code == 200 and key not in response.text
    for name in ["member", "foreign"]:
        bad = {**credentials[name], "X-Nexus-Subject": "user:admin"}
        assert client.post(BASE + "/authorize", headers=bad, json=auth_check).status_code == 403
        assert client.get(BASE + "/grants/" + grant["id"], headers=bad).status_code == 403
    # A delegated worker cannot become its own issuer/gateway via headers.
    assert (
        client.post(
            BASE + "/authorize",
            headers={"Authorization": "Bearer " + key, "X-Nexus-Zone-ID": "zone-a"},
            json=auth_check,
        ).status_code
        == 403
    )
    response = client.post(BASE + "/grants/" + grant["id"] + "/revoke", headers=headers)
    assert response.status_code == 200 and response.json()["cleanup_pending"] is False
    assert client.post(BASE + "/authorize", headers=headers, json=auth_check).status_code == 401


def test_http_invalid_input_never_echoes_secret(api, execution):
    client, credentials = api
    headers = credentials["operator"]
    for content in [
        b'{"token":"do-not-echo","unknown":"secret"}',
        b'{"kind":"a","kind":"b"}',
        b"[]",
        b"{",
    ]:
        response = client.post(BASE + "/authorize", headers=headers, content=content)
        assert response.status_code == 400 and "do-not-echo" not in response.text
    assert (
        client.post(BASE + "/authorize", headers=headers, content=b"x" * 65537).status_code == 413
    )
    assert client.post(BASE + "/grants", json=request(execution).model_dump()).status_code == 401


def test_binding_get_has_no_implicit_issuance_or_migration(api, execution):
    client, credentials = api
    result = asyncio.run(execution.issue(ADMIN, request(execution)))
    statements = []

    def capture(_con, _cursor, sql, *_args):
        statements.append(sql.strip().split()[0].upper())

    # Auth legitimately updates last_used_at; track only the product tables.
    def product_only(con, cursor, sql, *args):
        if "product_agent_" in sql:
            capture(con, cursor, sql, *args)

    event.listen(execution.engine, "before_cursor_execute", product_only)
    try:
        response = client.get(BASE + "/grants/" + result.grant.id, headers=credentials["operator"])
        assert response.status_code == 200
    finally:
        event.remove(execution.engine, "before_cursor_execute", product_only)
    assert not {"INSERT", "UPDATE", "DELETE", "ALTER", "CREATE"} & set(statements)


def test_revoke_request_http_has_no_credential_or_identity_override(api, execution):
    client, credentials = api
    req = request(execution)
    command = {
        "kind": "RevokeExecutionRequest",
        "request_id": req.request_id,
        "task_id": req.task_id,
    }
    for who in ["member", "foreign"]:
        assert (
            client.post(
                BASE + "/requests/revoke", headers=credentials[who], json=command
            ).status_code
            == 403
        )
    response = client.post(BASE + "/requests/revoke", headers=credentials["operator"], json=command)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["grants"] == [] and not response.json()["cleanup_pending"]
    assert (
        client.post(
            BASE + "/grants", headers=credentials["operator"], json=req.model_dump()
        ).status_code
        == 409
    )
    assert (
        client.post(
            BASE + "/requests/revoke",
            headers=credentials["operator"],
            json={**command, "zone": "other"},
        ).status_code
        == 400
    )
