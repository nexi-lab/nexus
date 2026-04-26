"""Admin RPC handler zone filters route through junction (#3871)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.server.rpc.handlers import admin
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def auth_provider_and_keys(tmp_path):
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

    # require_database_auth checks for SOMETHING — inspect the function signature
    # if your fake provider needs additional attrs. Most checks just look for
    # `session_factory`.
    auth_provider = SimpleNamespace(session_factory=_factory)
    context = SimpleNamespace(is_admin=True, user_id="admin")
    return auth_provider, context, SessionLocal, multi_id, eng_id


def test_handle_admin_list_keys_zone_filter_uses_junction(auth_provider_and_keys):
    """Filter by zone='ops' must return the multi-zone key (its primary is 'eng')."""
    auth_provider, context, _SessionLocal, multi_id, _eng_id = auth_provider_and_keys
    params = SimpleNamespace(
        zone_id="ops",
        user_id=None,
        is_admin=None,
        include_revoked=False,
        include_expired=False,
        limit=100,
        offset=0,
    )
    result = admin.handle_admin_list_keys(auth_provider, params, context)
    keys = result.get("keys", result) if isinstance(result, dict) else result
    assert {k["key_id"] for k in keys} == {multi_id}


def test_handle_admin_get_key_zone_filter_uses_junction(auth_provider_and_keys):
    """Multi-zone key fetched with zone='ops' must succeed (primary is 'eng')."""
    auth_provider, context, _SessionLocal, multi_id, _eng_id = auth_provider_and_keys
    params = SimpleNamespace(key_id=multi_id, zone_id="ops")
    result = admin.handle_admin_get_key(auth_provider, params, context)
    assert result["key_id"] == multi_id


def test_handle_admin_update_key_zone_filter_uses_junction(auth_provider_and_keys):
    """Multi-zone key updated with zone='ops' (no field changes) must succeed."""
    auth_provider, context, _SessionLocal, multi_id, _eng_id = auth_provider_and_keys
    params = SimpleNamespace(
        key_id=multi_id,
        zone_id="ops",
        name=None,
        is_admin=None,
        expires_days=None,
    )
    result = admin.handle_admin_update_key(auth_provider, params, context)
    assert result["key_id"] == multi_id


def test_handle_admin_update_key_self_demotion_guard_uses_junction(auth_provider_and_keys):
    """Self-demotion guard counts admins in the caller's zone-set, not via api_key.zone_id.

    Set up: two admin keys, both in `eng`. Demoting one should be allowed
    (the other still admins `eng`). Before the migration, the guard reads
    api_key.zone_id which is the primary; after, it reads the junction
    via get_zones_for_key.
    """
    auth_provider, context, SessionLocal, _multi_id, _eng_id = auth_provider_and_keys
    with SessionLocal() as s:
        admin_a, _ = create_api_key(s, user_id="u1", name="admin_a", zones=["eng"], is_admin=True)
        admin_b, _ = create_api_key(s, user_id="u1", name="admin_b", zones=["eng"], is_admin=True)
        s.commit()

    params = SimpleNamespace(
        key_id=admin_a,
        zone_id=None,
        name=None,
        is_admin=False,
        expires_days=None,
    )
    # Should succeed — admin_b is still admin in `eng`.
    result = admin.handle_admin_update_key(auth_provider, params, context)
    assert result["key_id"] == admin_a


def test_self_demotion_guard_blocks_last_multi_zone_admin(auth_provider_and_keys):
    """Guard must fire when the sole admin has multiple junction rows (#3871).

    Regression test for the count(DISTINCT key_id) fix — `select(count()).distinct()`
    counts join rows, not keys. For a sole admin in 2 zones, count(*) returns 2,
    which would incorrectly satisfy `> 1` and allow demotion to zero admins.
    """
    from nexus.contracts.exceptions import ValidationError

    auth_provider, context, SessionLocal, _multi_id, _eng_id = auth_provider_and_keys
    with SessionLocal() as s:
        sole_id, _ = create_api_key(
            s, user_id="u1", name="sole", zones=["eng", "ops"], is_admin=True
        )
        s.commit()

    params = SimpleNamespace(
        key_id=sole_id,
        zone_id=None,
        name=None,
        is_admin=False,
        expires_days=None,
    )
    with pytest.raises(ValidationError, match="last admin key"):
        admin.handle_admin_update_key(auth_provider, params, context)
