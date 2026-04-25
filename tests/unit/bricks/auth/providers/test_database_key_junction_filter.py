"""list_keys zone filter must match every junction row, not just primary (#3871)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        yield s


def test_list_keys_zone_filter_matches_every_granted_zone(session):
    multi_id, _ = create_api_key(session, user_id="u1", name="multi", zones=["eng", "ops"])
    eng_only, _ = create_api_key(session, user_id="u1", name="eng_only", zones=["eng"])

    rows_eng = DatabaseAPIKeyAuth.list_keys(session, zone_id="eng")
    rows_ops = DatabaseAPIKeyAuth.list_keys(session, zone_id="ops")

    assert {r["key_id"] for r in rows_eng} == {multi_id, eng_only}
    assert {r["key_id"] for r in rows_ops} == {multi_id}
