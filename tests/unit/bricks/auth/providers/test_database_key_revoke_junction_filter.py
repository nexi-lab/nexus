"""DatabaseAPIKeyAuth.revoke_key zone filter must match every junction row (#3871)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel
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


def test_revoke_key_zone_filter_matches_multi_zone_key_via_junction(session):
    """A multi-zone key whose primary is 'eng' must be revocable when caller scopes to 'ops'."""
    multi_id, _ = create_api_key(session, user_id="u1", name="multi", zones=["eng", "ops"])

    revoked = DatabaseAPIKeyAuth.revoke_key(session, multi_id, zone_id="ops")
    assert revoked is True

    refreshed = session.get(APIKeyModel, multi_id)
    assert refreshed.revoked == 1


def test_revoke_key_zone_filter_rejects_non_member(session):
    """Caller scoped to a zone the key doesn't grant must NOT revoke."""
    eng_only, _ = create_api_key(session, user_id="u1", name="eng_only", zones=["eng"])

    revoked = DatabaseAPIKeyAuth.revoke_key(session, eng_only, zone_id="ops")
    assert revoked is False

    refreshed = session.get(APIKeyModel, eng_only)
    assert refreshed.revoked == 0
