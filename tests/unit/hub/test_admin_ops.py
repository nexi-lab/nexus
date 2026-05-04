from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.hub import admin_ops
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        for zone_id in ("eng", "ops"):
            session.add(ZoneModel(zone_id=zone_id, name=zone_id, phase="Active"))
        session.commit()

    @contextmanager
    def _factory():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    return _factory


def test_create_hub_token_supports_multi_zone_permissions(session_factory):
    result = admin_ops.create_hub_token(
        session_factory,
        name="remote-admin",
        zones_csv="eng:r,ops:rw",
        zones_glob=None,
        is_admin=True,
        expires=None,
        user_id=None,
    )

    assert result["key_id"]
    assert result["token"].startswith("sk-")
    assert result["name"] == "remote-admin"
    assert result["admin"] is True
    assert result["zones"] == [
        {"zone_id": "eng", "permission": "r"},
        {"zone_id": "ops", "permission": "rw"},
    ]


def test_list_hub_tokens_returns_local_cli_payload_shape(session_factory):
    with session_factory() as session:
        key_id, _ = create_api_key(
            session,
            user_id="alice",
            name="alice-token",
            zones=["eng", ("ops", "rw")],
            is_admin=False,
        )
        session.commit()

    payload = admin_ops.list_hub_tokens(session_factory, show_revoked=False)

    assert payload["tokens"][0]["key_id"] == key_id
    assert payload["tokens"][0]["name"] == "alice-token"
    assert payload["tokens"][0]["zone"] == "eng"
    assert payload["tokens"][0]["zones"] == ["eng", "ops"]


def test_revoke_hub_token_matches_local_message(session_factory):
    with session_factory() as session:
        key_id, _ = create_api_key(session, user_id="alice", name="revoke-me", zones=["eng"])
        session.commit()

    result = admin_ops.revoke_hub_token(session_factory, identifier=key_id[:12])

    assert result["key_id"] == key_id
    assert result["name"] == "revoke-me"
    assert (
        result["message"] == f"revoked revoke-me ({key_id}). Effective within 60s (auth cache TTL)."
    )


def test_get_hub_status_reports_postgres_and_token_counts(session_factory, monkeypatch):
    monkeypatch.setenv("NEXUS_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("NEXUS_MCP_PORT", "8081")

    with session_factory() as session:
        create_api_key(session, user_id="admin", name="admin", is_admin=True)
        session.commit()

    payload = admin_ops.get_hub_status(session_factory, redis_stats=lambda: {"redis": "n/a"})

    assert payload["postgres"] == "ok"
    assert payload["tokens"] == {"active": 1, "revoked": 0}
    assert payload["endpoint"] == "http://127.0.0.1:8081/mcp"
