"""Legacy zone_perms fallback removed in Phase 2 (#3871)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.api_key_ops import create_api_key, hash_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import APIKeyModel, ZoneModel


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/no_fallback.db")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.commit()
    return sf


def _make_auth(session_factory) -> DatabaseAPIKeyAuth:
    rs = SimpleNamespace(session_factory=session_factory)
    return DatabaseAPIKeyAuth(record_store=rs)


def _insert_legacy_key(session_factory, *, key_id, raw_token, zone_id, is_admin=0):
    """Insert a key in the pre-junction shape (zone_id set, no junction rows)."""
    with session_factory() as s:
        s.add(
            APIKeyModel(
                key_id=key_id,
                key_hash=hash_api_key(raw_token),
                user_id="u1",
                name="legacy",
                zone_id=zone_id,
                is_admin=is_admin,
            )
        )
        s.commit()


def test_non_admin_legacy_zone_scoped_key_rejected(session_factory):
    """Legacy non-admin key with zone_id col but no junction MUST fail closed (#3871 round 2)."""
    raw = "sk-legacy_noadmin_k1_abcdefghijklm"
    _insert_legacy_key(session_factory, key_id="kid_k1", raw_token=raw, zone_id="eng")
    auth = _make_auth(session_factory)
    result = asyncio.run(auth.authenticate(raw))
    assert result.authenticated is False


def test_admin_key_zoneless_authenticates_with_empty_perms(session_factory):
    """Truly zoneless admin (zone_id IS NULL, no junction) authenticates as global admin."""
    raw = "sk-legacy_admin_k2_abcdefghijklmno"
    _insert_legacy_key(session_factory, key_id="kid_k2", raw_token=raw, zone_id=None, is_admin=1)
    auth = _make_auth(session_factory)
    result = asyncio.run(auth.authenticate(raw))
    assert result.authenticated is True
    assert result.is_admin is True
    assert tuple(result.zone_perms) == ()


def test_legacy_zone_scoped_admin_rejected(session_factory):
    """Pre-Phase-2 admin with zone_id col but no junction MUST fail closed —
    otherwise it gets silently reinterpreted as a global/zoneless admin
    (privilege escalation, #3871 round 2)."""
    raw = "sk-legacy_zoned_admin_abcdefghijkl"
    _insert_legacy_key(session_factory, key_id="kid_zadm", raw_token=raw, zone_id="eng", is_admin=1)
    auth = _make_auth(session_factory)
    result = asyncio.run(auth.authenticate(raw))
    assert result.authenticated is False


def test_multi_zone_key_uses_junction_primary_for_zone_id(session_factory):
    """After Task 6, api_key.zone_id is NULL; result.zone_id must come from the junction primary."""
    with session_factory() as s:
        s.add(ZoneModel(zone_id="ops", name="ops", phase="Active"))
        s.commit()
        _, raw = create_api_key(s, user_id="u1", name="multi", zones=["eng", "ops"])
        s.commit()

    auth = _make_auth(session_factory)
    result = asyncio.run(auth.authenticate(raw))
    assert result.authenticated is True
    # MIN granted_at picks "eng" (first inserted); zone_set contains both.
    assert result.zone_id == "eng"
    assert set(result.zone_set) == {"eng", "ops"}
