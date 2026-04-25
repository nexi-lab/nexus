"""create_api_key accepts a zone list and writes junction rows (#3785)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel, APIKeyZoneModel, ZoneModel
from nexus.storage.models._base import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.add(ZoneModel(zone_id="ops", name="ops", phase="Active"))
        s.commit()
        yield s


def test_single_zone_creates_one_junction_row(session, monkeypatch):
    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    key_id, _ = create_api_key(
        session,
        user_id="alice",
        name="alice",
        zones=["eng"],
    )
    session.commit()

    junction = (
        session.execute(select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == key_id))
        .scalars()
        .all()
    )
    assert [r.zone_id for r in junction] == ["eng"]
    primary = session.get(APIKeyModel, key_id)
    assert primary.zone_id == "eng"


def test_multi_zone_creates_one_junction_row_per_zone(session, monkeypatch):
    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    key_id, _ = create_api_key(
        session,
        user_id="alice",
        name="alice",
        zones=["eng", "ops"],
    )
    session.commit()

    junction = (
        session.execute(
            select(APIKeyZoneModel)
            .where(APIKeyZoneModel.key_id == key_id)
            .order_by(APIKeyZoneModel.zone_id)
        )
        .scalars()
        .all()
    )
    assert [r.zone_id for r in junction] == ["eng", "ops"]
    primary = session.get(APIKeyModel, key_id)
    assert primary.zone_id == "eng"  # first in zones list


def test_zone_id_legacy_kwarg_still_works(session, monkeypatch):
    """Backward-compat for callers that still pass single zone_id."""
    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    key_id, _ = create_api_key(
        session,
        user_id="alice",
        name="alice",
        zone_id="eng",
    )
    session.commit()

    junction = (
        session.execute(select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == key_id))
        .scalars()
        .all()
    )
    assert [r.zone_id for r in junction] == ["eng"]
