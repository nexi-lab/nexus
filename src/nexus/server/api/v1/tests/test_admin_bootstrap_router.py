"""Unit tests for /v1/admin/daemon-bootstrap (dev-only, #3804).

Tests use a module-scoped engine; each test uses a UUID-suffixed tenant
name to avoid cross-test collisions and deadlocks when xdist workers
share the same Postgres schema.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.postgres_profile_store import ensure_schema
from nexus.server.api.v1.routers.admin_bootstrap import make_admin_bootstrap_router


def _decode_enroll_body(token: str) -> dict[str, Any]:
    """Decode the URL-safe b64-encoded JSON body of an enroll token (no verify)."""
    import base64
    import json

    body_b64 = token.split(".")[0]
    pad = "=" * (-len(body_b64) % 4)
    parsed: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body_b64 + pad).decode())
    return parsed


PG_URL = "postgresql+psycopg2://postgres:nexus@localhost:5432/nexus"

# Serialize with other schema-mutating tests on the same Postgres so that
# concurrent `ensure_schema()` calls don't deadlock on AccessExclusiveLock.
pytestmark = [pytest.mark.xdist_group("postgres_auth_profile_store")]


@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("NEXUS_ALLOW_ADMIN_BYPASS", "true")
    app = FastAPI()
    app.include_router(
        make_admin_bootstrap_router(engine=engine, enroll_secret=b"x" * 32, admin_user="admin")
    )
    return TestClient(app)


def test_bootstrap_rejects_without_admin_bypass(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production deployments (bypass=false) must see 404."""
    monkeypatch.delenv("NEXUS_ALLOW_ADMIN_BYPASS", raising=False)
    app = FastAPI()
    app.include_router(
        make_admin_bootstrap_router(engine=engine, enroll_secret=b"x" * 32, admin_user="admin")
    )
    c = TestClient(app)
    r = c.post("/v1/admin/daemon-bootstrap", headers={"X-Admin-User": "admin"}, json={})
    assert r.status_code == 404


def test_bootstrap_rejects_wrong_admin_user(client: TestClient) -> None:
    r = client.post("/v1/admin/daemon-bootstrap", headers={"X-Admin-User": "someone-else"}, json={})
    assert r.status_code == 401


def test_bootstrap_mints_tenant_principal_and_token(client: TestClient, engine: Engine) -> None:
    """Happy path: creates tenant + principal, returns decodable enroll token."""
    tn = f"bootstrap-test-{uuid.uuid4()}"
    r = client.post(
        "/v1/admin/daemon-bootstrap",
        headers={"X-Admin-User": "admin"},
        json={"tenant_name": tn, "principal_label": "laptop-a", "ttl_minutes": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    tid = uuid.UUID(body["tenant_id"])
    pid = uuid.UUID(body["principal_id"])

    # Token body decodes + matches the returned IDs.
    claims = _decode_enroll_body(body["enroll_token"])
    assert uuid.UUID(claims["tid"]) == tid
    assert uuid.UUID(claims["pid"]) == pid

    # Row exists in DB for both the tenant and the principal.
    with engine.begin() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id = :t"), {"t": str(tid)}
        ).scalar()
        assert n == 1
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tid)})
        n = conn.execute(
            text("SELECT COUNT(*) FROM principals WHERE id = :p AND tenant_id = :t"),
            {"p": str(pid), "t": str(tid)},
        ).scalar()
        assert n == 1


def test_bootstrap_is_idempotent_on_label(client: TestClient) -> None:
    """Second call with same tenant/label returns the SAME principal_id."""
    tn = f"idem-{uuid.uuid4()}"
    r1 = client.post(
        "/v1/admin/daemon-bootstrap",
        headers={"X-Admin-User": "admin"},
        json={"tenant_name": tn, "principal_label": "same-laptop"},
    )
    r2 = client.post(
        "/v1/admin/daemon-bootstrap",
        headers={"X-Admin-User": "admin"},
        json={"tenant_name": tn, "principal_label": "same-laptop"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Same tenant + same label = same principal (not a fresh one each time).
    assert r1.json()["tenant_id"] == r2.json()["tenant_id"]
    assert r1.json()["principal_id"] == r2.json()["principal_id"]
    # But different single-use enroll tokens (jti is fresh each call).
    assert r1.json()["enroll_token"] != r2.json()["enroll_token"]
