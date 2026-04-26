"""SQLAlchemyAPIKeyStore.create_key must not write APIKeyModel.zone_id; populates junction (#3871)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.storage.api_key_ops import get_zones_for_key
from nexus.storage.auth_stores.sqlalchemy_api_key_store import SQLAlchemyAPIKeyStore
from nexus.storage.models import APIKeyModel
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def store_and_factory(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.commit()
    return SQLAlchemyAPIKeyStore(session_factory=SessionLocal), SessionLocal


def test_create_key_writes_null_zone_id_with_zone_arg(store_and_factory):
    store, SessionLocal = store_and_factory
    dto = store.create_key(key_hash="h1", user_id="u1", name="k", zone_id="eng")
    with SessionLocal() as s:
        row = s.get(APIKeyModel, dto.key_id)
        assert row.zone_id is None  # column not used
        assert get_zones_for_key(s, dto.key_id) == ["eng"]  # junction populated


def test_create_key_writes_null_zone_id_zoneless(store_and_factory):
    store, SessionLocal = store_and_factory
    dto = store.create_key(key_hash="h2", user_id="u1", name="root")  # no zone_id
    with SessionLocal() as s:
        row = s.get(APIKeyModel, dto.key_id)
        assert row.zone_id is None
        assert get_zones_for_key(s, dto.key_id) == []  # no junction row
