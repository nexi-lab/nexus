"""REST create-key response `zone_id` field equals get_primary_zone (#3871)."""

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
def app_with_auth(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()

    @contextmanager
    def _factory():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    fake_db_provider = SimpleNamespace(session_factory=_factory)

    from nexus.server.api.v2.routers import auth_keys as auth_keys_module

    monkeypatch.setattr(auth_keys_module, "_resolve_db_auth", lambda request: fake_db_provider)

    app = FastAPI()
    app.include_router(auth_keys_module.router)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(is_admin=True)
    app.add_exception_handler(NexusError, nexus_error_handler)

    return TestClient(app)


def test_rest_create_key_response_emits_primary_in_zone_id_field(app_with_auth):
    """create_key response zone_id comes from get_primary_zone (junction), not legacy column."""
    resp = app_with_auth.post("/api/v2/auth/keys", json={"name": "alice", "zone_id": "eng"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # zone_id must be sourced from the junction (get_primary_zone), not result.get("zone_id")
    assert body["zone_id"] == "eng"
