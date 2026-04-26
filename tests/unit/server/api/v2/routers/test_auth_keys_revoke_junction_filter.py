"""REST revoke_key zone filter routes through junction (#3871)."""

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
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def app_and_keys(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        multi_id, _ = create_api_key(s, user_id="u1", name="multi", zones=["eng", "ops"])
        eng_id, _ = create_api_key(s, user_id="u1", name="eng_only", zones=["eng"])
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

    # require_admin IS a Depends, so dependency_overrides is the right tool.
    app = FastAPI()
    app.include_router(auth_keys_module.router)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(is_admin=True)
    app.add_exception_handler(NexusError, nexus_error_handler)

    return TestClient(app), SessionLocal, multi_id, eng_id


def test_rest_revoke_key_zone_filter_matches_multi_zone_key(app_and_keys):
    client, SessionLocal, multi_id, _eng_id = app_and_keys
    # Multi-zone key: primary "eng"; scoping revoke to "ops" must succeed via junction.
    resp = client.request("DELETE", f"/api/v2/auth/keys/{multi_id}", params={"zone_id": "ops"})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as s:
        assert s.get(APIKeyModel, multi_id).revoked == 1


def test_rest_revoke_key_zone_filter_rejects_non_member(app_and_keys):
    client, SessionLocal, _multi_id, eng_id = app_and_keys
    # Single-zone "eng" key scoped to "ops": junction miss → 404, key stays unrevoked.
    resp = client.request("DELETE", f"/api/v2/auth/keys/{eng_id}", params={"zone_id": "ops"})
    assert resp.status_code == 404, resp.text
    with SessionLocal() as s:
        assert s.get(APIKeyModel, eng_id).revoked == 0
