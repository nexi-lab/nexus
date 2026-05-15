"""create_api_key must not write APIKeyModel.zone_id (#3871)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import APIKeyModel, ZoneModel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        yield s


def test_create_api_key_writes_null_zone_id_for_single_zone_key(session):
    key_id, _ = create_api_key(session, user_id="u1", name="alice", zones=["eng"])
    row = session.get(APIKeyModel, key_id)
    assert row.zone_id is None


def test_create_api_key_writes_null_zone_id_for_multi_zone_key(session):
    key_id, _ = create_api_key(session, user_id="u1", name="alice", zones=["eng", "ops"])
    row = session.get(APIKeyModel, key_id)
    assert row.zone_id is None


def test_create_api_key_writes_null_zone_id_for_zoneless_admin_key(session):
    # Zoneless = omit both `zones` and `zone_id` (passing `zones=[]` raises ValueError).
    key_id, _ = create_api_key(session, user_id="u1", name="root", is_admin=True)
    row = session.get(APIKeyModel, key_id)
    assert row.zone_id is None
