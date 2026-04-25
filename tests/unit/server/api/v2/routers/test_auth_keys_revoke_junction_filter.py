"""REST revoke_key zone filter routes through junction (#3871)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel, APIKeyZoneModel
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


def test_rest_revoke_key_zone_filter_matches_multi_zone_key(monkeypatch, tmp_path):
    """Multi-zone key should be revocable when scoped to non-primary zone via junction."""
    # Setup: file-based SQLite engine (memory DBs have per-connection isolation issues)
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # Create test data
    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        multi_id, _ = create_api_key(s, user_id="u1", name="multi", zones=["eng", "ops"])
        s.commit()

    # Verify data exists
    with SessionLocal() as s:
        api_key = s.get(APIKeyModel, multi_id)
        assert api_key is not None
        zones = (
            s.execute(select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == multi_id))
            .scalars()
            .all()
        )
        assert len(zones) == 2

    # Setup: fake db_provider + monkeypatch _resolve_db_auth BEFORE importing router
    @contextmanager
    def session_factory():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    fake_db_provider = SimpleNamespace(session_factory=session_factory)

    # Patch before creating the app
    import nexus.server.api.v2.routers.auth_keys as auth_keys_module

    monkeypatch.setattr(
        auth_keys_module,
        "_resolve_db_auth",
        lambda request: fake_db_provider,
    )

    # Get the router from the already-imported module (don't reload, or we'll lose the patch)
    router = auth_keys_module.router

    # Create app and override require_admin dependency
    app = FastAPI()
    app.include_router(router)

    # Add exception handler for NexusFileNotFoundError
    @app.exception_handler(NexusFileNotFoundError)
    async def nexus_file_not_found_handler(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # Override require_admin to return admin auth result
    from nexus.server.dependencies import require_admin

    app.dependency_overrides[require_admin] = lambda: {"is_admin": True}

    client = TestClient(app)

    # Act: revoke multi-zone key scoped to non-primary zone
    resp = client.request("DELETE", f"/api/v2/auth/keys/{multi_id}", params={"zone_id": "ops"})

    # Assert: revoke succeeded (200)
    assert resp.status_code == 200, resp.text

    # Verify: key is now revoked
    with SessionLocal() as s:
        refreshed = s.get(APIKeyModel, multi_id)
        assert refreshed.revoked == 1


def test_rest_revoke_key_zone_filter_rejects_non_member(monkeypatch, tmp_path):
    """Single-zone key should NOT be revocable when scoped to non-member zone."""
    # Setup: file-based SQLite engine (memory DBs have per-connection isolation issues)
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # Create test data
    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        eng_id, _ = create_api_key(s, user_id="u1", name="eng_only", zones=["eng"])
        s.commit()

    # Verify data exists
    with SessionLocal() as s:
        api_key = s.get(APIKeyModel, eng_id)
        assert api_key is not None
        zones = (
            s.execute(select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == eng_id))
            .scalars()
            .all()
        )
        assert len(zones) == 1

    # Setup: fake db_provider + monkeypatch _resolve_db_auth BEFORE importing router
    @contextmanager
    def session_factory():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    fake_db_provider = SimpleNamespace(session_factory=session_factory)

    # Patch before creating the app
    import nexus.server.api.v2.routers.auth_keys as auth_keys_module

    monkeypatch.setattr(
        auth_keys_module,
        "_resolve_db_auth",
        lambda request: fake_db_provider,
    )

    # Get the router from the already-imported module (don't reload, or we'll lose the patch)
    router = auth_keys_module.router

    # Create app and override require_admin dependency
    app = FastAPI()
    app.include_router(router)

    # Add exception handler for NexusFileNotFoundError
    @app.exception_handler(NexusFileNotFoundError)
    async def nexus_file_not_found_handler(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # Override require_admin to return admin auth result
    from nexus.server.dependencies import require_admin

    app.dependency_overrides[require_admin] = lambda: {"is_admin": True}

    client = TestClient(app)

    # Act: try to revoke eng-only key scoped to ops (non-member)
    resp = client.request("DELETE", f"/api/v2/auth/keys/{eng_id}", params={"zone_id": "ops"})
    # Should get 404 (key not found in that zone); either way the key must not be revoked.
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    # Verify: key is still NOT revoked
    with SessionLocal() as s:
        refreshed = s.get(APIKeyModel, eng_id)
        assert refreshed.revoked == 0
