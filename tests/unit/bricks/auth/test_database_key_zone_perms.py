"""DatabaseAPIKeyAuth populates AuthResult.zone_perms from junction (#3785 F3c)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel, ZoneModel
from nexus.storage.models._base import Base


def _record_store(sf: sessionmaker) -> MagicMock:
    rs = MagicMock()
    rs.session_factory = sf
    return rs


@pytest.fixture()
def session_factory(tmp_path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path}/zp.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_authenticate_loads_mixed_zone_perms(session_factory: sessionmaker) -> None:
    """Junction with mixed perms -> AuthResult.zone_perms preserves them."""
    with session_factory() as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.add(ZoneModel(zone_id="ops", name="ops", phase="Active"))
        s.add(ZoneModel(zone_id="legal", name="legal", phase="Active"))
        s.commit()
        _, raw_key = create_api_key(
            s,
            user_id="alice",
            name="alice",
            zones=[("eng", "r"), ("ops", "rw"), ("legal", "rwx")],
        )
        s.commit()

    auth = DatabaseAPIKeyAuth(record_store=_record_store(session_factory))
    result = asyncio.run(auth.authenticate(raw_key))

    assert result.authenticated is True
    perms = dict(result.zone_perms)
    assert perms == {"eng": "r", "ops": "rw", "legal": "rwx"}
    # zone_set is derived from zone_perms by AuthResult.__post_init__.
    assert sorted(result.zone_set) == ["eng", "legal", "ops"]


def test_authenticate_default_perms_when_zones_only(session_factory: sessionmaker) -> None:
    """Bare zone strings (no perm) default to 'rw' in the junction."""
    with session_factory() as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.add(ZoneModel(zone_id="ops", name="ops", phase="Active"))
        s.commit()
        _, raw_key = create_api_key(s, user_id="alice", name="alice", zones=["eng", "ops"])
        s.commit()

    auth = DatabaseAPIKeyAuth(record_store=_record_store(session_factory))
    result = asyncio.run(auth.authenticate(raw_key))

    assert result.authenticated is True
    assert dict(result.zone_perms) == {"eng": "rw", "ops": "rw"}


def test_authenticate_legacy_token_rejected(session_factory: sessionmaker) -> None:
    """Legacy single-zone token (zone_id col set, no junction rows) MUST fail closed
    (#3871 round 2 — strict junction-only auth; tripwire migration enforces backfill)."""
    raw_key = "sk-legacy_perm_test_abcdefghijklmnop"
    auth = DatabaseAPIKeyAuth(record_store=_record_store(session_factory))
    key_hash = auth._hash_key(raw_key)

    with session_factory() as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.add(
            APIKeyModel(
                key_id="kid_legacy_perm",
                key_hash=key_hash,
                user_id="legacy",
                name="legacy",
                zone_id="eng",
            )
        )
        s.commit()

    result = asyncio.run(auth.authenticate(raw_key))
    assert result.authenticated is False
