"""Zone-list CRUD helpers for tokens (#3785)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import (
    add_zone_to_key,
    create_api_key,
    get_zones_for_key,
    remove_zone_from_key,
)
from nexus.storage.models import ZoneModel
from nexus.storage.models._base import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for z in ("eng", "ops", "legal"):
            s.add(ZoneModel(zone_id=z, name=z, phase="Active"))
        s.commit()
        yield s


def test_get_zones_for_key_returns_set(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng", "ops"])
    session.commit()
    assert sorted(get_zones_for_key(session, key_id)) == ["eng", "ops"]


def test_add_zone_inserts_junction_row(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng"])
    session.commit()

    added = add_zone_to_key(session, key_id, "ops")
    session.commit()
    assert added is True
    assert sorted(get_zones_for_key(session, key_id)) == ["eng", "ops"]


def test_add_zone_idempotent(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng"])
    session.commit()

    added = add_zone_to_key(session, key_id, "eng")
    session.commit()
    assert added is False  # already present


def test_remove_zone_deletes_junction_row(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng", "ops"])
    session.commit()

    removed = remove_zone_from_key(session, key_id, "ops")
    session.commit()
    assert removed is True
    assert get_zones_for_key(session, key_id) == ["eng"]


def test_remove_zone_refuses_last_zone(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng"])
    session.commit()

    with pytest.raises(ValueError, match="last zone"):
        remove_zone_from_key(session, key_id, "eng")


def test_remove_unknown_zone_returns_false(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng", "ops"])
    session.commit()
    assert remove_zone_from_key(session, key_id, "legal") is False
