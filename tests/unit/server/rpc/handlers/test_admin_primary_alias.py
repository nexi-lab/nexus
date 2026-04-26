"""admin echo `zone_id` field equals get_primary_zone (#3871)."""

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
def auth_provider_and_key(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        key_id, _ = create_api_key(s, user_id="u1", name="multi", zones=["eng", "ops"])
        s.commit()

    @contextmanager
    def _factory():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    auth_provider = SimpleNamespace(session_factory=_factory)
    context = SimpleNamespace(is_admin=True, user_id="admin")
    return auth_provider, context, key_id


def test_admin_get_key_echoes_primary_zone(auth_provider_and_key):
    auth_provider, context, key_id = auth_provider_and_key
    params = SimpleNamespace(key_id=key_id, zone_id=None)
    result = admin.handle_admin_get_key(auth_provider, params, context)
    assert result["zone_id"] == "eng"  # primary by granted_at
