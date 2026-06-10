"""REST create_key surfaces zone-lifecycle guard failures as structured 400s (#4352).

The #3871 guard in DatabaseAPIKeyAuth.create_key raises ValueError when the
target zone is missing, Terminating/Terminated, or soft-deleted. The REST
route must translate that into an HTTPException(400) with the cause in
``detail`` — not let it escape as an unstructured plain-text 500.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import NexusError
from nexus.server.dependencies import require_admin
from nexus.server.error_handlers import nexus_error_handler
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as s:
        s.add(ZoneModel(zone_id="zone_active", name="zone_active", phase="Active"))
        s.add(ZoneModel(zone_id="zone_terminated", name="zone_terminated", phase="Terminated"))
        s.commit()

    @contextmanager
    def _factory():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    fake_db_provider = SimpleNamespace(session_factory=_factory)

    # _resolve_db_auth is a plain function called inside the handler (not a Depends),
    # so we monkeypatch the symbol on the router module rather than using
    # app.dependency_overrides.
    from nexus.server.api.v2.routers import auth_keys as auth_keys_module

    monkeypatch.setattr(auth_keys_module, "_resolve_db_auth", lambda request: fake_db_provider)

    app = FastAPI()
    app.include_router(auth_keys_module.router)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(is_admin=True)
    app.add_exception_handler(NexusError, nexus_error_handler)

    # raise_server_exceptions=False mirrors prod: an uncaught exception becomes
    # Starlette's plain-text 500 instead of re-raising into the test.
    return TestClient(app, raise_server_exceptions=False)


def _create_body(zone_id: str | None, **overrides) -> dict:
    body = {
        "label": "koodle team key",
        "user_id": "team-user",
        "subject_type": "agent",
        "subject_id": "team-agent",
        "is_admin": False,
    }
    if zone_id is not None:
        body["zone_id"] = zone_id
    body.update(overrides)
    return body


def test_create_key_missing_zone_returns_structured_400(client):
    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_ghost"))
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "zone_ghost" in detail
    assert "is not active" in detail


def test_create_key_terminated_zone_returns_structured_400(client):
    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_terminated"))
    assert resp.status_code == 400, resp.text
    assert "is not active" in resp.json()["detail"]


def test_create_key_active_zone_still_succeeds(client):
    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_active"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["zone_id"] == "zone_active"
    assert body["key"].startswith("sk-")
