"""APIKeyZoneModel — junction table for token → zone allow-list (#3785)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexus.storage.models import APIKeyModel, APIKeyZoneModel, ZoneModel
from nexus.storage.models._base import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_junction_row_inserts_and_loads(session):
    session.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
    session.add(ZoneModel(zone_id="ops", name="ops", phase="Active"))
    session.add(
        APIKeyModel(
            key_id="kid_1",
            key_hash="hash_1",
            user_id="alice",
            name="alice",
            zone_id="eng",
        )
    )
    session.commit()

    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="eng"))
    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="ops"))
    session.commit()

    rows = (
        session.execute(select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == "kid_1"))
        .scalars()
        .all()
    )
    zones = sorted(r.zone_id for r in rows)
    assert zones == ["eng", "ops"]
    assert all(isinstance(r.granted_at, datetime) for r in rows)


def test_composite_pk_prevents_duplicate(session):
    session.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
    session.add(
        APIKeyModel(
            key_id="kid_1",
            key_hash="hash_1",
            user_id="alice",
            name="alice",
            zone_id="eng",
        )
    )
    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="eng"))
    session.commit()

    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="eng"))
    with pytest.raises(IntegrityError):  # duplicate composite PK
        session.commit()
