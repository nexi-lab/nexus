"""revoke_key zone filter routes through junction (#3871)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.storage.api_key_ops import create_api_key
from nexus.storage.auth_stores.sqlalchemy_api_key_store import SQLAlchemyAPIKeyStore
from nexus.storage.models import APIKeyModel
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def store_and_keys(tmp_path):
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

    store = SQLAlchemyAPIKeyStore(session_factory=SessionLocal)
    return store, SessionLocal, multi_id, eng_id


def test_revoke_key_zone_filter_matches_multi_zone_key(store_and_keys):
    store, SessionLocal, multi_id, _eng_id = store_and_keys
    # Multi-zone key: primary "eng"; scoping revoke to "ops" must succeed via junction.
    revoked = store.revoke_key(multi_id, zone_id="ops")
    assert revoked is True
    with SessionLocal() as s:
        assert s.get(APIKeyModel, multi_id).revoked == 1


def test_revoke_key_zone_filter_rejects_non_member(store_and_keys):
    store, SessionLocal, _multi_id, eng_id = store_and_keys
    # Single-zone "eng" key scoped to "ops": junction miss → False, key stays unrevoked.
    revoked = store.revoke_key(eng_id, zone_id="ops")
    assert revoked is False
    with SessionLocal() as s:
        assert s.get(APIKeyModel, eng_id).revoked == 0
