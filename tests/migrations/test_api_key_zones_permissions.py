"""api_key_zones.permissions column round-trips and defaults to 'rw' (#3785)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nexus.storage.models import APIKeyZoneModel, ZoneModel
from nexus.storage.models._base import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.commit()
        yield s


def test_permissions_defaults_to_rw_when_omitted(session):
    session.add(APIKeyZoneModel(key_id="kid_default", zone_id="eng"))
    session.commit()

    row = session.execute(
        select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == "kid_default")
    ).scalar_one()
    assert row.permissions == "rw"


def test_permissions_round_trips_read_only(session):
    session.add(APIKeyZoneModel(key_id="kid_r", zone_id="eng", permissions="r"))
    session.commit()

    row = session.execute(
        select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == "kid_r")
    ).scalar_one()
    assert row.permissions == "r"


def test_permissions_round_trips_admin_shorthand(session):
    session.add(APIKeyZoneModel(key_id="kid_admin", zone_id="eng", permissions="rwx"))
    session.commit()

    row = session.execute(
        select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == "kid_admin")
    ).scalar_one()
    assert row.permissions == "rwx"
