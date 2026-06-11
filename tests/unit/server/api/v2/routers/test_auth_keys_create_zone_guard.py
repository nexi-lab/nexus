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


@pytest.fixture
def record_store_client(monkeypatch):
    """Client backed by a real record store so the entity-registry path runs."""
    from tests.testkit.records import InMemoryRecordStore

    store = InMemoryRecordStore()
    with store.session_factory() as s:
        s.add(ZoneModel(zone_id="zone_active", name="zone_active", phase="Active"))
        s.commit()

    provider = SimpleNamespace(session_factory=store.session_factory, _record_store=store)

    from nexus.server.api.v2.routers import auth_keys as auth_keys_module

    monkeypatch.setattr(auth_keys_module, "_resolve_db_auth", lambda request: provider)

    app = FastAPI()
    app.include_router(auth_keys_module.router)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(is_admin=True)
    app.add_exception_handler(NexusError, nexus_error_handler)

    yield TestClient(app, raise_server_exceptions=False), store
    store.close()


def test_failed_create_leaves_no_entity_registry_row(record_store_client):
    """A 400 from the zone guard must not commit identity state: a stale
    entity_registry row parented to the bad zone would make a later valid
    create skip registration and keep the wrong parent forever."""
    from sqlalchemy import select

    from nexus.storage.models.memory import EntityRegistryModel

    client, store = record_store_client

    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_ghost"))
    assert resp.status_code == 400, resp.text

    with store.session_factory() as s:
        rows = s.scalars(select(EntityRegistryModel)).all()
        assert rows == [], f"stale entity rows after failed create: {rows}"

    # Subsequent valid create must register the entity under the active zone
    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_active"))
    assert resp.status_code == 201, resp.text

    with store.session_factory() as s:
        entity = s.scalars(
            select(EntityRegistryModel).where(EntityRegistryModel.entity_id == "team-agent")
        ).one()
        assert entity.parent_id == "zone_active"


def test_invalid_subject_type_leaves_no_entity_registry_row(record_store_client):
    """'zone' is a valid entity_registry type but not a valid key subject_type;
    the rejection must come before the entity row is committed."""
    from sqlalchemy import select

    from nexus.storage.models.memory import EntityRegistryModel

    client, store = record_store_client

    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_active", subject_type="zone"))
    assert resp.status_code == 400, resp.text
    assert "subject_type" in resp.json()["detail"]

    with store.session_factory() as s:
        rows = s.scalars(select(EntityRegistryModel)).all()
        assert rows == [], f"stale entity rows after rejected subject_type: {rows}"


def test_registration_failure_does_not_fail_created_key(record_store_client, monkeypatch):
    """Registration runs only after the key commit and is best-effort: a
    registry failure must not turn an already-issued key into an error
    response (the row self-heals on the next create)."""
    from nexus.bricks.rebac.entity_registry import EntityRegistry

    client, _store = record_store_client

    def boom(self, *args, **kwargs):
        raise RuntimeError("simulated registry failure")

    monkeypatch.setattr(EntityRegistry, "register_entity_if_absent", boom)

    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_active"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["key"].startswith("sk-")


def test_non_valueerror_create_failure_leaves_no_entity_row(record_store_client, monkeypatch):
    """Any key-creation failure — not just the ValueError guards — must leave
    no entity row behind (registration only runs after a successful commit)."""
    from sqlalchemy import select

    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models.memory import EntityRegistryModel

    client, store = record_store_client

    def boom(cls_session, *args, **kwargs):
        raise RuntimeError("simulated driver failure")

    monkeypatch.setattr(DatabaseAPIKeyAuth, "create_key", classmethod(boom))

    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_active"))
    assert resp.status_code == 500

    with store.session_factory() as s:
        rows = s.scalars(select(EntityRegistryModel)).all()
        assert rows == [], f"stale entity rows after non-ValueError failure: {rows}"


def test_failed_create_never_deletes_preexisting_entity(record_store_client):
    """Ownership: a row registered by an earlier request must survive this
    request's failure — failed creates never touch the registry."""
    from sqlalchemy import select

    from nexus.bricks.rebac.entity_registry import EntityRegistry
    from nexus.storage.models.memory import EntityRegistryModel

    client, store = record_store_client

    # Another request already registered the subject under the active zone.
    EntityRegistry(store).register_entity(
        "agent", "team-agent", parent_type="zone", parent_id="zone_active"
    )

    resp = client.post("/api/v2/auth/keys", json=_create_body("zone_ghost"))
    assert resp.status_code == 400, resp.text

    with store.session_factory() as s:
        entity = s.scalars(
            select(EntityRegistryModel).where(EntityRegistryModel.entity_id == "team-agent")
        ).one_or_none()
        assert entity is not None, "failed create deleted another request's entity row"
        assert entity.parent_id == "zone_active"
